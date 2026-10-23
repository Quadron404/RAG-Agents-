/**
 * Bindings and vars the Worker reads.
 *
 * Secrets (GOOGLE_CLIENT_SECRET, RAG_INTERNAL_TOKEN, SESSION_SECRET) are set
 * with `wrangler secret put` and are never committed.  The URLs and the client
 * id are plain vars so a different Google client or a local stub can be pointed
 * at without a rebuild.
 */
/**
 * The subset of the AI Search Workers binding this Worker uses.
 *
 * The generated Cloudflare types for this binding change more often than the
 * four methods below, so the contract is stated locally.  It keeps the chat
 * code compiling against the same shape the deploy uses while leaving the
 * actual binding supplied by Wrangler.
 */
export interface AiSearchChunk {
  id?: string;
  text?: string;
  score?: number;
  item?: { key?: string; metadata?: Record<string, unknown> };
}

export interface AiSearchInstance {
  search(options: Record<string, unknown>): Promise<{ chunks?: AiSearchChunk[] }>;
  info(): Promise<Record<string, unknown>>;
  update(options: Record<string, unknown>): Promise<unknown>;
  items: {
    upload(
      name: string,
      content: string | Blob | ReadableStream,
      options?: { metadata?: Record<string, string> },
    ): Promise<{ id?: string; key?: string }>;
    delete?(name: string): Promise<unknown>;
  };
}

export interface AiSearchNamespace {
  get(name: string): AiSearchInstance;
  create(options: Record<string, unknown>): Promise<AiSearchInstance>;
}

export interface Env {
  DB: D1Database;
  /** Static asset binding for the built frontend. Absent when only APIs run. */
  ASSETS?: Fetcher;
  /** AI Search namespace binding used for conversation summaries. */
  AI_SEARCH?: AiSearchNamespace;
  /** Instance inside AI_SEARCH that stores conversation summaries. */
  AI_SEARCH_INSTANCE?: string;

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

  // --- Worker-native chat ---------------------------------------------------
  /** groq | openai | anthropic | gemini | mistral | openrouter. */
  AGENT_PROVIDER?: string;
  AGENT_MODEL?: string;
  AGENT_FALLBACK_MODEL?: string;
  AGENT_MAX_COMPLETION_TOKENS?: string;
  AGENT_REASONING_EFFORT?: string;
  /** Optional summary override.  Defaults to the agent's fallback model. */
  SUMMARY_PROVIDER?: string;
  SUMMARY_MODEL?: string;
  /** Seconds of conversation inactivity before a summary is requested. */
  CHAT_SUMMARY_IDLE_SECONDS?: string;
  /** Maximum history-tool calls in one user turn. */
  CHAT_MAX_TOOL_CALLS?: string;

  GROQ_API_KEY?: string;
  GROQ_BASE_URL?: string;
  OPENAI_API_KEY?: string;
  OPENAI_BASE_URL?: string;
  ANTHROPIC_API_KEY?: string;
  ANTHROPIC_BASE_URL?: string;
  GEMINI_API_KEY?: string;
  GEMINI_BASE_URL?: string;
  MISTRAL_API_KEY?: string;
  MISTRAL_BASE_URL?: string;
  OPENROUTER_API_KEY?: string;
  OPENROUTER_BASE_URL?: string;
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
