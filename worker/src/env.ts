/**
 * Bindings and vars the Worker reads.
 *
 * Secrets (GOOGLE_CLIENT_SECRET, RAG_INTERNAL_TOKEN, SESSION_SECRET) are set
 * with `wrangler secret put` and are never committed.  The URLs and the client
 * id are plain vars so a different Google client or a local stub can be pointed
 * at without a rebuild.
 */
export interface Env {
  DB: D1Database;
  /** Static asset binding for the built frontend. Absent when only APIs run. */
  ASSETS?: Fetcher;

  GOOGLE_CLIENT_ID: string;
  GOOGLE_CLIENT_SECRET: string;
  GOOGLE_AUTH_URL: string;
  GOOGLE_TOKEN_URL: string;
  GOOGLE_JWKS_URL: string;
  /**
   * A literal JWKS document. When set it is used instead of fetching
   * GOOGLE_JWKS_URL, which is how tests verify real RS256 tokens without a
   * socket.
   */
  GOOGLE_JWKS_JSON?: string;
  /** Override the accepted token issuers (comma separated). Testing seam. */
  GOOGLE_ISSUERS?: string;

  RAG_INTERNAL_TOKEN: string;
  SESSION_SECRET: string;
  SESSION_TTL_SECONDS?: string;

  /** Public origin of the Codespace backend the Worker proxies to. */
  BACKEND_ORIGIN: string;
  /** The stable workers.dev origin; derived from the request when empty. */
  PUBLIC_BASE_URL?: string;
  /** Set to "1" only for local http development so the cookie is not Secure. */
  ALLOW_INSECURE_COOKIE?: string;

  // --- Durable routines: Codespace lifecycle ---------------------------------
  // A routine may be due while the Codespace is stopped, so the scheduler has
  // to be able to wake it.  The token is a fine-grained PAT (Codespaces: read
  // and write) supplied with `wrangler secret put`; the name identifies which
  // Codespace to start.  When either is missing the scheduler still runs -- it
  // simply leaves runs queued and says so, rather than pretending it woke
  // something.
  GITHUB_TOKEN?: string;
  /** Override the GitHub API base (a testing seam, and GitHub Enterprise). */
  GITHUB_API_URL?: string;
  /** The Codespace to start, by name (e.g. "fluffy-space-giggle-abc123"). */
  CODESPACE_NAME?: string;
  /** How long to wait for the backend to answer /health after a wake, in ms. */
  CODESPACE_READY_TIMEOUT_MS?: string;
  /** How often to re-probe the backend while waiting for it, in ms. */
  CODESPACE_POLL_INTERVAL_MS?: string;
  /** Per-run attempt ceiling before a retryable failure becomes terminal. */
  ROUTINE_MAX_ATTEMPTS?: string;
  /** Runs dispatched in one scheduler tick. */
  ROUTINE_DISPATCH_BATCH?: string;
  /** Base for exponential backoff after a retryable failure, in ms. */
  ROUTINE_RETRY_BASE_MS?: string;
}

export const COOKIE_NAME = "rag_session";
export const CSRF_HEADER = "x-rag-csrf";
export const INTERNAL_HEADER = "x-rag-internal-token";

/** Methods that never change state, so they need no CSRF token. */
export const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);

// Identity the Worker asserts about the signed-in user when it proxies.  The
// backend trusts these only after it has checked the internal token, so a
// client must never be able to supply them.
export const IDENTITY_SUB_HEADER = "x-rag-user-sub";
export const IDENTITY_EMAIL_HEADER = "x-rag-user-email";
export const IDENTITY_EMAIL_VERIFIED_HEADER = "x-rag-user-email-verified";
export const IDENTITY_NAME_HEADER = "x-rag-user-name";
export const IDENTITY_PICTURE_HEADER = "x-rag-user-picture";

/** Headers a client must never be able to set on a proxied request. */
export const SPOOFABLE_HEADERS = [
  INTERNAL_HEADER,
  IDENTITY_SUB_HEADER,
  IDENTITY_EMAIL_HEADER,
  IDENTITY_EMAIL_VERIFIED_HEADER,
  IDENTITY_NAME_HEADER,
  IDENTITY_PICTURE_HEADER,
  "x-rag-user-id",
  "cookie",
];

export const DEFAULT_TTL_SECONDS = 60 * 60 * 24 * 30;
