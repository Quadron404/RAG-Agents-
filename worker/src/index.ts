/**
 * The stable workers.dev application.
 *
 * Two responsibilities:
 *
 *   1. Serve the built frontend and own Google sign-in.  The Worker runs the
 *      authorization-code + PKCE flow with `openid email profile` only, stores
 *      users and sessions in D1, and issues an HttpOnly session cookie.  Gmail
 *      is never requested here, so login works with no mailbox authorization.
 *
 *   2. Proxy the API and WebSocket paths to the Codespace backend, injecting
 *      the shared internal token and the authenticated identity so the backend
 *      can be a plain resource server behind the tunnel.
 */
import type { Env } from "./env";
import { COOKIE_NAME, CSRF_HEADER, SAFE_METHODS } from "./env";
import { timingSafeEqual } from "./encoding";
import {
  clearSessionCookie,
  isSecureRequest,
  json,
  publicBaseUrl,
  redirect,
  safeNext,
  serializeSessionCookie,
  sessionTokenFromRequest,
} from "./http";
import { OAuthError, buildAuthorizationUrl, exchangeCode, fetchJwks, newCodeVerifier, newNonce, newState } from "./google";
import { handleInternal } from "./internal";
import { resolvePrincipal, type Principal } from "./principal";
import { buildUpstreamRequest } from "./proxy";
import { runScheduler } from "./scheduler";
import { TokenError, verifyIdToken } from "./idtoken";
import {
  auditLogin,
  consumeOauthState,
  createOauthState,
  createSession,
  hashSessionToken,
  revokeSession,
  upsertUser,
} from "./store";

const API_PREFIXES = ["/threads", "/computer", "/ai", "/sysinfo", "/file", "/cdp", "/screen", "/websockify", "/ws", "/routines"];

function isProxyPath(path: string): boolean {
  if (path === "/health") return true;
  if (path.startsWith("/auth/")) return true; // e.g. /auth/gmail/* is the backend's later grant
  return API_PREFIXES.some((prefix) => path === prefix || path.startsWith(`${prefix}/`));
}

async function requireCsrf(request: Request, principal: Principal): Promise<Response | null> {
  const supplied = request.headers.get(CSRF_HEADER) ?? "";
  const expected = principal.session.csrf ?? "";
  if (!expected || !timingSafeEqual(supplied, expected)) {
    return json({ error: "invalid CSRF token" }, 403);
  }
  return null;
}

async function authLogin(request: Request, env: Env, url: URL): Promise<Response> {
  if (!env.GOOGLE_CLIENT_ID || !env.GOOGLE_CLIENT_SECRET) {
    return json({ error: "Google sign-in is not configured" }, 503);
  }
  const redirectUri = `${publicBaseUrl(env, url)}/auth/callback`;
  const state = newState();
  const nonce = newNonce();
  const verifier = newCodeVerifier();
  await createOauthState(env, {
    state,
    mode: "login",
    codeVerifier: verifier,
    nonce,
    redirectUri,
    next: safeNext(url.searchParams.get("next")),
  });
  const location = await buildAuthorizationUrl(env, { redirectUri, state, nonce, verifier });
  return redirect(location);
}

async function authCallback(request: Request, env: Env, url: URL): Promise<Response> {
  const base = publicBaseUrl(env, url);
  if (url.searchParams.get("error")) {
    return redirect(`${base}/?auth=denied`);
  }
  const state = url.searchParams.get("state") ?? "";
  const code = url.searchParams.get("code") ?? "";
  if (!state || !code) {
    return json({ error: "missing state or code" }, 400);
  }
  const record = await consumeOauthState(env, state);
  if (!record || record.mode !== "login") {
    await auditLogin(env, "", "", "rejected", "invalid_or_reused_state");
    return json({ error: "invalid or expired sign-in state" }, 400);
  }

  let claims: Record<string, unknown>;
  try {
    const tokens = await exchangeCode(env, {
      code,
      redirectUri: record.redirect_uri,
      verifier: record.code_verifier,
    });
    const idToken = typeof tokens.id_token === "string" ? tokens.id_token : "";
    if (!idToken) {
      await auditLogin(env, "", "", "rejected", "no_id_token");
      return json({ error: "no identity token returned" }, 400);
    }
    const jwks = await fetchJwks(env);
    const issuers = env.GOOGLE_ISSUERS
      ? env.GOOGLE_ISSUERS.split(",").map((value) => value.trim()).filter(Boolean)
      : undefined;
    claims = await verifyIdToken(idToken, {
      jwks,
      audience: env.GOOGLE_CLIENT_ID,
      nonce: record.nonce,
      issuers,
    });
  } catch (error) {
    if (error instanceof OAuthError || error instanceof TokenError) {
      await auditLogin(env, "", "", "rejected", "invalid_id_token");
      return json({ error: "sign-in could not be verified" }, 400);
    }
    throw error;
  }

  const user = await upsertUser(env, {
    sub: String(claims.sub),
    email: claims.email,
    email_verified: claims.email_verified,
    name: claims.name,
    picture: claims.picture,
  });
  await auditLogin(env, String(claims.sub), user.id, "success");

  const existing = sessionTokenFromRequest(request);
  const rotatedFrom = existing ? await hashSessionToken(env, existing) : undefined;
  const session = await createSession(env, user.id, rotatedFrom);

  const response = redirect(`${base}${safeNext(record.next)}`);
  response.headers.append(
    "Set-Cookie",
    serializeSessionCookie(session.token, {
      maxAge: Math.floor((session.expiresAt - Date.now()) / 1000),
      secure: isSecureRequest(env, url),
    }),
  );
  return response;
}

async function authSession(request: Request, env: Env): Promise<Response> {
  const principal = await resolvePrincipal(env, request);
  if (!principal) {
    return json({ authenticated: false, user: null, gmail: { connected: false, status: "unknown" } });
  }
  return json({
    authenticated: true,
    csrf: principal.session.csrf,
    user: {
      id: principal.user.id,
      email: principal.user.email,
      name: principal.user.name,
      picture: principal.user.picture,
      timezone: "UTC",
    },
    // The edge does not own the mailbox connection: the backend does, and it is
    // the only thing that can say whether a Gmail grant is live.  Rather than
    // report a stale "not connected", the session says the state is unknown and
    // the UI reads GET /auth/gmail/status (proxied to the backend) for the truth.
    gmail: { connected: false, status: "unknown" },
  });
}

async function authLogout(request: Request, env: Env, url: URL): Promise<Response> {
  const principal = await resolvePrincipal(env, request);
  if (principal) {
    const failure = await requireCsrf(request, principal);
    if (failure) return failure;
    await revokeSession(env, sessionTokenFromRequest(request));
  }
  const response = json({ ok: true });
  response.headers.append(
    "Set-Cookie",
    clearSessionCookie({ secure: isSecureRequest(env, url) }),
  );
  return response;
}

async function proxyApi(
  request: Request,
  env: Env,
  url: URL,
  options: { requireAuth?: boolean } = {},
): Promise<Response> {
  const requireAuth = options.requireAuth ?? true;
  if (!env.BACKEND_ORIGIN) {
    return json({ error: "the backend is not configured" }, 503);
  }
  let principal: Principal | null = null;
  if (requireAuth) {
    principal = await resolvePrincipal(env, request);
    if (!principal) {
      return json({ error: "authentication required" }, 401);
    }
    // The edge owns CSRF for everything it proxies.  The backend cannot check a
    // token it never issued once the browser cookie is stripped on the way
    // through, so this is the only place a state-changing API call can be
    // guarded -- and it must be, or the backend would have to trust a
    // same-site cookie it never sees.
    if (!SAFE_METHODS.has(request.method)) {
      const failure = await requireCsrf(request, principal);
      if (failure) return failure;
    }
  }
  const upstream = buildUpstreamRequest(request, env, url, principal);
  return fetch(upstream);
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    const path = url.pathname;

    if (path === "/auth/login") return authLogin(request, env, url);
    if (path === "/auth/callback") return authCallback(request, env, url);
    if (path === "/auth/session") return authSession(request, env);
    if (path === "/auth/logout") return authLogout(request, env, url);

    // The backend's own routes.  Handled here, never proxied, and behind the
    // shared internal token.
    if (path.startsWith("/internal/")) {
      const handled = await handleInternal(request, env, url);
      if (handled) return handled;
    }

    if (path === "/health") return proxyApi(request, env, url, { requireAuth: false });
    if (isProxyPath(path)) return proxyApi(request, env, url);

    if (env.ASSETS) {
      return env.ASSETS.fetch(request);
    }
    return new Response("Not found", { status: 404 });
  },

  /**
   * The Cron Trigger entry point -- the one durable scheduler.
   *
   * It runs whether or not the Codespace is up, which is the whole point: due
   * routines are materialised in D1, the Codespace is woken only when there is
   * real work, and runs that cannot dispatch yet stay queued for a later tick.
   */
  async scheduled(_event: ScheduledController, env: Env, ctx: ExecutionContext): Promise<void> {
    ctx.waitUntil(runScheduler(env, Date.now()));
  },
};

export { COOKIE_NAME, CSRF_HEADER };
