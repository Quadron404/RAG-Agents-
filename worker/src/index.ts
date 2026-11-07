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
import { handleChatRoutes, isChatPath } from "./chat/routes";
import { removeSummaryDoc } from "./chat/search";
import { resolvePrincipal, type Principal } from "./principal";
import { buildUpstreamRequest } from "./proxy";
import { runScheduler } from "./scheduler";
import { TokenError, verifyIdToken } from "./idtoken";
import { validateTimezone } from "./timezone";
import {
  createRoutine,
  deleteRoutine,
  getRoutine,
  listRoutines,
  listRuns,
  setRoutineEnabled,
  updateRoutine,
} from "./routines";
import { listPendingApprovals, resolveConnectorApproval, getConnectorApproval } from "./connector-approvals";
import { executeApprovedConnectorAction } from "./chat/connector-tools";
import {
  auditLogin,
  consumeOauthState,
  createOauthState,
  createSession,
  getDeveloperMode,
  getUserTimezone,
  hashSessionToken,
  revokeSession,
  setDeveloperMode,
  setUserTimezone,
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
      timezone: principal.user.timezone ?? "UTC",
      developer_mode: principal.user.developer_mode ?? 0,
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

async function requireSettingsCsrf(request: Request, principal: Principal): Promise<Response | null> {
  const supplied = request.headers.get(CSRF_HEADER) ?? "";
  const expected = principal.session.csrf ?? "";
  if (!expected || !timingSafeEqual(supplied, expected)) {
    return json({ error: "invalid CSRF token" }, 403);
  }
  return null;
}

async function authSetTimezone(request: Request, env: Env): Promise<Response> {
  const principal = await resolvePrincipal(env, request);
  if (!principal) return json({ error: "authentication required" }, 401);
  const failure = await requireSettingsCsrf(request, principal);
  if (failure) return failure;
  let body: { timezone?: unknown } = {};
  try {
    body = (await request.json()) ?? {};
  } catch {
    /* ignore */
  }
  const tz = validateTimezone(body.timezone);
  const ok = await setUserTimezone(env, principal.user.id, tz);
  if (!ok) return json({ error: "user not found" }, 404);
  return json({ ok: true, timezone: tz });
}

async function authSetDeveloperMode(request: Request, env: Env): Promise<Response> {
  const principal = await resolvePrincipal(env, request);
  if (!principal) return json({ error: "authentication required" }, 401);
  const failure = await requireSettingsCsrf(request, principal);
  if (failure) return failure;
  let body: { developer_mode?: unknown } = {};
  try {
    body = (await request.json()) ?? {};
  } catch {
    /* ignore */
  }
  const enabled = body.developer_mode === true || body.developer_mode === 1 || body.developer_mode === "1";
  const ok = await setDeveloperMode(env, principal.user.id, enabled);
  if (!ok) return json({ error: "user not found" }, 404);
  return json({ ok: true, developer_mode: enabled ? 1 : 0 });
}

async function authResetAccount(request: Request, env: Env): Promise<Response> {
  const principal = await resolvePrincipal(env, request);
  if (!principal) return json({ error: "authentication required" }, 401);
  const failure = await requireSettingsCsrf(request, principal);
  if (failure) return failure;

  const userId = principal.user.id;
  const ownerSub = principal.user.google_sub;

  const conversations = await env.DB.prepare(
    `SELECT id FROM conversations WHERE owner_sub = ?1`,
  )
    .bind(ownerSub)
    .all<{ id: string }>();

  // Delete the user's channel-owned AI Search documents first.  Failures are
  // recorded and reported rather than swallowed, so the reset never claims
  // success while indexed data remains reachable.
  const indexErrors: string[] = [];
  for (const conversation of conversations.results ?? []) {
    const result = await removeSummaryDoc(env, ownerSub, conversation.id);
    if (!result.ok && result.error) {
      indexErrors.push(`${conversation.id}: ${result.error}`);
    }
  }

  await env.DB.batch([
    env.DB.prepare("DELETE FROM sessions WHERE user_id = ?1").bind(userId),
    env.DB.prepare("DELETE FROM login_audit WHERE user_id = ?1").bind(userId),
    // Deleting conversations cascades to messages, chat requests, daily/monthly/
    // yearly summaries, summary jobs and idle conversation summaries.
    env.DB.prepare("DELETE FROM conversations WHERE owner_sub = ?1").bind(ownerSub),
    env.DB.prepare("DELETE FROM users WHERE id = ?1").bind(userId),
  ]);

  await revokeSession(env, sessionTokenFromRequest(request));

  const response = json({
    ok: true,
    deletedConversations: (conversations.results ?? []).length,
    indexErrors,
    note:
      indexErrors.length > 0
        ? "Some channel AI Search documents could not be deleted immediately; they are orphaned and will be retried."
        : undefined,
  });
  response.headers.append(
    "Set-Cookie",
    clearSessionCookie({ secure: isSecureRequest(env, new URL(request.url)) }),
  );
  return response;
}

async function authQaLoginQuick(
  request: Request,
  env: Env,
  url: URL,
): Promise<Response> {
  // AI Testing entry point: a fixed, deterministic QA automation identity that
  // does not touch Google OAuth.  The same google_sub is reused on every call
  // so the QA session and its conversations persist across refreshes.  This is
  // purely a testing shortcut and is entirely separate from the normal Google
  // sign-in flow, which is left untouched.
  const base = publicBaseUrl(env, url);
  const next = safeNext(url.searchParams.get("next"));
  const user = await upsertUser(env, {
    sub: "qa-automation",
    email: "qa-automation@rag-agents.test",
    email_verified: true,
    name: "QA Automation",
    picture: "",
  });
  await setDeveloperMode(env, user.id, false);
  await setUserTimezone(env, user.id, "Asia/Calcutta");

  const existing = sessionTokenFromRequest(request);
  const rotatedFrom = existing ? await hashSessionToken(env, existing) : undefined;
  const session = await createSession(env, user.id, rotatedFrom);

  const response = redirect(`${base}${next}`);
  response.headers.append(
    "Set-Cookie",
    serializeSessionCookie(session.token, {
      maxAge: Math.floor((session.expiresAt - Date.now()) / 1000),
      secure: isSecureRequest(env, url),
    }),
  );
  await auditLogin(env, user.google_sub, user.id, "success", "qa-automation");
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

    if (path === "/auth/settings/timezone") {
      if (request.method !== "POST") return json({ error: "method not allowed" }, 405);
      return authSetTimezone(request, env);
    }

    if (path === "/auth/settings/developer-mode") {
      if (request.method !== "POST") return json({ error: "method not allowed" }, 405);
      return authSetDeveloperMode(request, env);
    }

    if (path === "/auth/settings/reset") {
      if (request.method !== "POST") return json({ error: "method not allowed" }, 405);
      return authResetAccount(request, env);
    }

    if (path === "/auth/qa-login-quick") {
      if (request.method !== "GET") return json({ error: "method not allowed" }, 405);
      return authQaLoginQuick(request, env, url);
    }

    // The backend's own routes.  Handled here, never proxied, and behind the
    // shared internal token.
    if (path.startsWith("/internal/")) {
      const handled = await handleInternal(request, env, url);
      if (handled) return handled;
    }

    if (path === "/health") return proxyApi(request, env, url, { requireAuth: false });

    // Chat transcripts and generation are Worker-owned.  Only the computer-use
    // and routine surfaces still cross the tunnel to the Codespace.
    if (isChatPath(path)) {
      const handled = await handleChatRoutes(request, env, url);
      if (handled) return handled;
    }

    // Worker-native connector approval endpoints.  These run entirely at the
    // edge -- no backend proxy.  Authentication comes from the session cookie.
    const approvalPath = /^\/routines\/connectors\/approvals(?:\/(execute|deny))?$/.exec(path);
    if (approvalPath) {
      const principal = await resolvePrincipal(env, request);
      if (!principal) {
        return json({ error: "authentication required" }, 401);
      }
      const userId = principal.user.id;

      if (path === "/routines/connectors/approvals" && request.method === "GET") {
        const approvals = await listPendingApprovals(env, userId, false);
        return json({
          ok: true,
          approvals: approvals.map((a) => ({
            approval_id: a.approval_id,
            connector: a.connector,
            action: a.action,
            arguments: a.arguments,
            status: a.status,
            expires_at: a.expires_at,
            created_at: a.created_at,
            routine_id: a.routine_id,
            routine_run_id: a.routine_run_id,
          })),
        });
      }

      if ((path === "/routines/connectors/approvals/execute" || path === "/routines/connectors/approvals/deny") && request.method === "POST") {
        let body: { approval_id?: unknown } = {};
        try {
          body = (await request.json()) ?? {};
        } catch {
          /* ignore */
        }
        const approvalId = typeof body.approval_id === "string" ? body.approval_id : "";
        if (!approvalId) {
          return json({ error: "approval_id is required" }, 400);
        }

        const action = approvalPath[1];
        if (action === "execute") {
          const result = await executeApprovedConnectorAction(env, approvalId, userId);
          if (!result.ok) {
            return json({ ok: false, error: result.error }, 400);
          }
          return json({ ok: true, result: result.result });
        }

        if (action === "deny") {
          const approval = await getConnectorApproval(env, approvalId);
          if (!approval) {
            return json({ error: "no such approval" }, 404);
          }
          if (approval.user_id !== userId) {
            return json({ error: "approval does not belong to you" }, 403);
          }
          if (approval.status !== "pending") {
            return json({ ok: false, error: `approval is ${approval.status}` }, 400);
          }
          if (Date.now() > approval.expires_at) {
            await resolveConnectorApproval(env, approvalId, { decision: "expired", decidedBy: userId, result: "expired before decision" });
            return json({ ok: false, error: "approval has expired" }, 400);
          }
          await resolveConnectorApproval(env, approvalId, { decision: "denied", decidedBy: userId, result: "denied by owner" });
          return json({ ok: true, status: "denied" });
        }
      }
    }

    // Worker-native routine listing.  Routines live in D1 at the edge and must
    // be readable whether or not the Codespace backend is reachable, so the
    // edge serves this directly (session-authenticated) instead of proxying
    // through the backend.  The backend's own /internal/routines path remains
    // its privileged client route.
    if (path === "/routines" && request.method === "GET") {
      const principal = await resolvePrincipal(env, request);
      if (!principal) return json({ error: "authentication required" }, 401);
      const ownerSub = principal.user.google_sub;
      if (!ownerSub) {
        return json({ ok: false, available: false, error: "This account has no routine identity.", routines: [] });
      }
      return json({ ok: true, available: true, routines: await listRoutines(env, ownerSub) });
    }

    // Worker-native routine run history and lifecycle.  Same reasoning as the
    // listing endpoint above: these run on D1 at the edge and should not depend
    // on the Codespace backend being awake.
    if (!path.startsWith("/routines/connectors/")) {
      const routineMatch = /^\/routines\/([^/]+)(\/(runs|pause|resume))?$/.exec(path);
      if (routineMatch) {
        const principal = await resolvePrincipal(env, request);
        if (!principal) return json({ error: "authentication required" }, 401);
        const ownerSub = principal.user.google_sub;
        if (!ownerSub) {
          return json({ ok: false, available: false, error: "This account has no routine identity.", routines: [] });
        }
        const routineId = decodeURIComponent(routineMatch[1]);
        const action = routineMatch[2] ? routineMatch[2].slice(1) : "";
        const routine = await getRoutine(env, routineId);
        if (!routine || routine.owner_sub !== ownerSub) {
          return json({ ok: false, error: "no such routine" }, 404);
        }
        const now = Date.now();
        if (action === "runs" && request.method === "GET") {
          const limit = Number(url.searchParams.get("limit") ?? "20") || 20;
          return json({ ok: true, runs: await listRuns(env, routineId, limit) });
        }
        if (action === "pause" && request.method === "POST") {
          return json({ ok: true, routine: await setRoutineEnabled(env, routineId, false, now) });
        }
        if (action === "resume" && request.method === "POST") {
          return json({ ok: true, routine: await setRoutineEnabled(env, routineId, true, now) });
        }
        if (request.method === "DELETE" && !action) {
          await deleteRoutine(env, routineId, now);
          return json({ ok: true });
        }
        return json({ ok: false, error: "method not allowed" }, 405);
      }
    }

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
