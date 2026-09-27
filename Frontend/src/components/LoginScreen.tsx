import { useEffect, useRef, useState } from "react";
import { KeyRound, Loader2, LogOut, ShieldCheck } from "lucide-react";

import { haptic } from "../lib/haptics";
import { useAuth } from "../lib/auth";
import { useCore } from "../core";

/**
 * The lock screen.
 *
 * Rendered instead of the app until a session exists.  Deliberately the only
 * thing shown when unauthenticated: no agent output, no conversation, no
 * Computer view, because every one of those is a way to reach the remote
 * machine.
 */
export function LoginScreen() {
  const login = useAuth((s) => s.login);
  const busy = useAuth((s) => s.busy);
  const error = useAuth((s) => s.error);
  const check = useAuth((s) => s.check);
  const [value, setValue] = useState("");
  const inputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  // A long-lived tab can be left open past the session's lifetime; re-checking
  // on focus means the user finds out at the door rather than staring at a
  // screen that quietly stopped updating.
  useEffect(() => {
    const onFocus = () => void check();
    window.addEventListener("focus", onFocus);
    return () => window.removeEventListener("focus", onFocus);
  }, [check]);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!value || busy) return;
    const ok = await login(value);
    if (ok) setValue("");
  };

  return (
    <div className="login">
      <form className="login__card" onSubmit={submit}>
        <div className="login__orb">
          <ShieldCheck size={30} />
        </div>

        <h1 className="login__title">RAG Agents</h1>
        <p className="login__sub">
          This workspace is published at a public URL and includes a live remote
          computer. Enter the passphrase to continue.
        </p>

        <label className="login__label" htmlFor="passphrase">
          Passphrase
        </label>
        <div className="login__field">
          <KeyRound size={16} />
          <input
            id="passphrase"
            ref={inputRef}
            type="password"
            value={value}
            autoComplete="current-password"
            placeholder="••••••••••••"
            onChange={(e) => setValue(e.target.value)}
            disabled={busy}
            aria-invalid={!!error}
          />
        </div>

        {error ? (
          <div className="login__error" role="alert">
            {error}
          </div>
        ) : null}

        <button className="btn btn--primary btn--lg login__submit" type="submit" disabled={busy || !value}>
          {busy ? <Loader2 size={17} className="spin" /> : null}
          {busy ? "Checking…" : "Sign in"}
        </button>
      </form>
    </div>
  );
}

/** The in-app way to end a session. */
export function SignOutButton() {
  const logout = useAuth((s) => s.logout);
  return (
    <button
      className="icon-btn"
      onClick={() => {
        haptic("medium");
        void logout();
      }}
      title="Sign out"
      aria-label="Sign out"
    >
      <LogOut size={16} />
    </button>
  );
}

/**
 * Decides between the app and the lock screen.
 *
 * `auth_required` comes from the server, so a deployment with no passphrase
 * configured still works locally instead of showing a login nobody can pass.
 */
export function AuthGate({ children }: { children: React.ReactNode }) {
  const checked = useAuth((s) => s.checked);
  const required = useAuth((s) => s.required);
  const authenticated = useAuth((s) => s.authenticated);
  const check = useAuth((s) => s.check);
  const status = useCore((s) => s.status);

  // Once a session exists, the app's own socket should be up; if it is not, the
  // session has almost certainly lapsed, so fall back to the lock screen.
  useEffect(() => {
    void check();
  }, [check]);

  if (!checked) {
    return (
      <div className="login">
        <div className="login__card">
          <Loader2 size={26} className="spin" />
        </div>
      </div>
    );
  }

  if (required && !authenticated) return <LoginScreen />;
  if (required && authenticated && status === "offline") return <LoginScreen />;
  return <>{children}</>;
}
