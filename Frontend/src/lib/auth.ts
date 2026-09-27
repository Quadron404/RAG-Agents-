/**
 * The sign-in gate.
 *
 * The app is published at a public Cloudflare Quick Tunnel URL, and the Computer
 * view behind it is a live, keyboard-driven, signed-in Google Chrome.  That URL
 * is not a secret -- it turns up in DNS, in proxy logs and in browser history --
 * so this passphrase is the thing that decides who gets in.
 *
 * The credential never reaches JavaScript after login: the server answers with
 * an HttpOnly cookie, and everything after this screen just sends credentials:
 * "include".  There is nowhere for the passphrase to leak from.
 */
import { create } from "zustand";

import { api } from "../core";

export interface AuthState {
  /** False until /auth/status has answered, so we never flash the login screen. */
  checked: boolean;
  required: boolean;
  authenticated: boolean;
  busy: boolean;
  error: string;
  check: () => Promise<void>;
  login: (passphrase: string) => Promise<boolean>;
  logout: () => Promise<void>;
}

export const useAuth = create<AuthState>((set, get) => ({
  checked: false,
  required: false,
  authenticated: false,
  busy: false,
  error: "",

  check: async () => {
    try {
      const res = await fetch(api("/auth/status"), { credentials: "include" });
      if (!res.ok) throw new Error(String(res.status));
      const d = await res.json();
      set({
        checked: true,
        required: !!d.auth_required,
        authenticated: !!d.authenticated,
        error: "",
      });
    } catch {
      // The backend is unreachable.  Do not claim success: stay locked.
      set({ checked: true, required: true, authenticated: false, error: "" });
    }
  },

  login: async (passphrase) => {
    if (get().busy) return false;
    set({ busy: true, error: "" });
    try {
      const res = await fetch(api("/auth/login"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify({ passphrase }),
      });
      if (res.ok) {
        set({ busy: false, authenticated: true, required: true, error: "" });
        return true;
      }
      const d = await res.json().catch(() => ({}));
      // 503 is a server misconfiguration, not a wrong passphrase, and telling
      // the user the difference is the only way they can fix it.
      set({
        busy: false,
        error:
          res.status === 503
            ? d.error || "This server has no passphrase configured."
            : "Incorrect passphrase.",
      });
      return false;
    } catch {
      set({ busy: false, error: "Could not reach the server." });
      return false;
    }
  },

  logout: async () => {
    try {
      await fetch(api("/auth/logout"), { method: "POST", credentials: "include" });
    } catch {
      /* the cookie is cleared either way on the next login */
    }
    set({ authenticated: false, required: true, error: "" });
  },
}));
