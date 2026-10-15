/** Cookie and response helpers shared by the auth routes and the proxy. */
import type { Env } from "./env";
import { COOKIE_NAME } from "./env";

export function parseCookies(header: string | null): Record<string, string> {
  const out: Record<string, string> = {};
  if (!header) return out;
  for (const part of header.split(";")) {
    const index = part.indexOf("=");
    if (index === -1) continue;
    const name = part.slice(0, index).trim();
    const value = part.slice(index + 1).trim();
    if (name) out[name] = value;
  }
  return out;
}

export function sessionTokenFromRequest(request: Request): string {
  return parseCookies(request.headers.get("cookie"))[COOKIE_NAME] ?? "";
}

export interface CookieOptions {
  maxAge?: number;
  secure?: boolean;
}

export function serializeSessionCookie(token: string, options: CookieOptions = {}): string {
  const parts = [
    `${COOKIE_NAME}=${token}`,
    "Path=/",
    "HttpOnly",
    "SameSite=Lax",
  ];
  if (options.secure) {
    parts.push("Secure");
  }
  if (options.maxAge !== undefined) {
    parts.push(`Max-Age=${Math.floor(options.maxAge)}`);
  }
  return parts.join("; ");
}

export function clearSessionCookie(options: CookieOptions = {}): string {
  const parts = [`${COOKIE_NAME}=`, "Path=/", "HttpOnly", "SameSite=Lax", "Max-Age=0"];
  if (options.secure) {
    parts.push("Secure");
  }
  return parts.join("; ");
}

/** Cookies are only marked Secure on an https origin, or when forced. */
export function isSecureRequest(env: Env, url: URL): boolean {
  if (env.ALLOW_INSECURE_COOKIE === "1") return false;
  return url.protocol === "https:";
}

/** The public origin OAuth redirects must be built against. */
export function publicBaseUrl(env: Env, url: URL): string {
  const configured = (env.PUBLIC_BASE_URL ?? "").trim().replace(/\/+$/, "");
  return configured || url.origin;
}

/**
 * Only a same-origin, absolute-path redirect is allowed.
 *
 * `//evil.com` and `https://evil.com` are both rejected so a crafted `next`
 * cannot turn the callback into an open redirect.
 */
export function safeNext(next: string | null | undefined): string {
  if (!next) return "/";
  if (!next.startsWith("/") || next.startsWith("//")) return "/";
  return next;
}

export function json(data: unknown, status = 200, headers: HeadersInit = {}): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8", ...headers },
  });
}

export function redirect(location: string, headers: HeadersInit = {}): Response {
  return new Response(null, { status: 302, headers: { Location: location, ...headers } });
}
