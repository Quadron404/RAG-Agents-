import { create } from "zustand";

/* ============================================================================
   Auth — the edge session.

   The Cloudflare Worker owns Google sign-in and the session cookie.  This
   module asks it who is signed in (GET /auth/session), remembers the per-session
   CSRF token every state-changing call must echo, and exposes the sign-in and
   sign-out transitions.  Nothing here holds a token: the credential is the
   HttpOnly cookie the browser already has.
   ========================================================================== */

export interface AuthUser {
  id: string;
  email: string;
  name: string;
  picture: string;
  timezone: string;
  developer_mode: number;
}

/**
 * The mailbox connection.  `status` is the backend's real state
 * (`disconnected` | `connected` | `error` | `revoked` | `reauth_required` |
 * `unknown`); `connected` is true only for a healthy connection, and
 * `needsReconnect` is true when the grant is gone and the user must consent
 * again.  This never carries a token.
 */
export interface GmailStatus {
  connected: boolean;
  email: string;
  status: string;
  needsReconnect: boolean;
  autoReply: boolean;
}

const DISCONNECTED: GmailStatus = {
  connected: false,
  email: "",
  status: "unknown",
  needsReconnect: false,
  autoReply: false,
};

interface AuthState {
  /** False until the first /auth/session answer arrives. */
  ready: boolean;
  authenticated: boolean;
  user: AuthUser | null;
  csrf: string;
  gmail: GmailStatus;
  bootstrap: () => Promise<void>;
  logout: () => Promise<void>;
  refreshGmail: () => Promise<void>;
  connectGmail: () => void;
  disconnectGmail: () => Promise<void>;
  setDeveloperMode: (enabled: boolean) => Promise<void>;
}

function normalizeGmail(raw: unknown): GmailStatus {
  if (!raw || typeof raw !== "object") return { ...DISCONNECTED };
  const g = raw as Partial<GmailStatus>;
  return {
    connected: Boolean(g.connected),
    email: String(g.email || ""),
    status: String(g.status || (g.connected ? "connected" : "unknown")),
    needsReconnect: Boolean(g.needsReconnect),
    autoReply: Boolean(g.autoReply),
  };
}

export const useAuth = create<AuthState>((set, get) => ({
  ready: false,
  authenticated: false,
  user: null,
  csrf: "",
  gmail: { ...DISCONNECTED },
  async bootstrap() {
    try {
      const res = await fetch("/auth/session", { credentials: "include" });
      const data = await res.json();
      if (data && data.authenticated) {
        const user = data.user as AuthUser;
        set({
          ready: true,
          authenticated: true,
          user,
          csrf: String(data.csrf || ""),
        });
        // Replace the server's UTC fallback with the browser's real IANA
        // timezone on first sign-in, so daily summaries use local calendar days.
        void persistDetectedTimezone(String(user.timezone || ""));
        // The mailbox state is owned by the backend, so it is read on its own.
        await get().refreshGmail();
        return;
      }
    } catch {
      /* treated as signed out below */
    }
    set({ ready: true, authenticated: false, user: null, csrf: "", gmail: { ...DISCONNECTED } });
  },

  async refreshGmail() {
    if (!get().authenticated) {
      set({ gmail: { ...DISCONNECTED } });
      return;
    }
    try {
      const res = await fetch("/auth/gmail/status", { credentials: "include" });
      if (!res.ok) throw new Error(String(res.status));
      set({ gmail: normalizeGmail(await res.json()) });
    } catch {
      // A status we could not read is reported as unknown, not as connected.
      set({ gmail: { ...DISCONNECTED } });
    }
  },

  connectGmail() {
    // A full-page navigation: consent must be a top-level Google flow, and the
    // callback returns to this app.  `prompt=consent` is added server-side so a
    // revoked grant re-prompts instead of silently doing nothing.
    location.assign("/auth/gmail/connect");
  },

  async disconnectGmail() {
    try {
      const res = await fetch("/auth/gmail/disconnect", {
        method: "POST",
        credentials: "include",
        headers: { "x-rag-csrf": get().csrf },
      });
      if (res.ok) {
        const data = await res.json();
        if (data && data.gmail) {
          set({ gmail: normalizeGmail(data.gmail) });
          return;
        }
      }
    } catch {
      /* fall through to a re-read */
    }
    await get().refreshGmail();
  },

  async logout() {
    try {
      await fetch("/auth/logout", {
        method: "POST",
        credentials: "include",
        headers: { "x-rag-csrf": get().csrf },
      });
    } catch {
      /* the cookie is cleared server-side; a failed call still signs out locally */
    }
    set({ authenticated: false, user: null, csrf: "", gmail: { ...DISCONNECTED } });
    location.assign("/");
  },

  async setDeveloperMode(enabled: boolean) {
    try {
      const res = await fetch("/auth/settings/developer-mode", {
        method: "POST",
        credentials: "include",
        headers: {
          "Content-Type": "application/json",
          "x-rag-csrf": get().csrf,
        },
        body: JSON.stringify({ developer_mode: enabled ? 1 : 0 }),
      });
      if (res.ok) {
        const data = await res.json();
        set((state) => ({
          user: state.user ? { ...state.user, developer_mode: data.developer_mode ?? Number(enabled) } : state.user,
        }));
      }
    } catch {
      /* best effort */
    }
  },
}));

/** The owner's IANA timezone, detected from the browser and persisted to the
 * backend.  ``UTC`` is the documented server-side fallback; a real browser
 * timezone replaces it on first sign-in so daily summaries use local calendar
 * days rather than UTC days. */
export function detectBrowserTimezone(): string {
  try {
    const tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
    return typeof tz === "string" && tz ? tz : "";
  } catch {
    return "";
  }
}

/** Persist the detected browser timezone, but only when the server still holds
 * the ``UTC`` fallback (i.e. the owner has not chosen one explicitly).  Best
 * effort: a failure leaves the server default in place and does not block sign
 * in. */
async function persistDetectedTimezone(current: string) {
  if (current && current !== "UTC") return;
  const tz = detectBrowserTimezone();
  if (!tz || tz === "UTC") return;
  try {
    await fetch("/auth/settings/timezone", {
      method: "POST",
      credentials: "include",
      headers: {
        "Content-Type": "application/json",
        "x-rag-csrf": useAuth.getState().csrf,
      },
      body: JSON.stringify({ timezone: tz }),
    });
  } catch {
    /* the server's UTC fallback remains; a later load can retry */
  }
}

/** The CSRF token for the current session, for the shared `apiFetch` helper. */
export function currentCsrf(): string {
  return useAuth.getState().csrf;
}

/** The signed-in user's id, or empty when there is no session. */
export function currentUserId(): string {
  return useAuth.getState().user?.id || "";
}

/** Start the Google flow, returning to the current page afterwards. */
export function loginHref(): string {
  const next = encodeURIComponent(location.pathname + location.search);
  return `/auth/login?next=${next}`;
}
