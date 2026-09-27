import { useEffect, useRef, useState } from "react";
import { KeyRound, Loader2, LogOut, RefreshCw, ShieldAlert, ShieldCheck } from "lucide-react";

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
 * Shown when the server has no passphrase configured.
 *
 * This state used to fall through to the app, on the assumption that a
 * deployment without a passphrase would simply work.  It does not: the backend
 * fails closed on purpose and refuses every route when RAG_AUTH_TOKEN is unset,
 * so the workspace rendered fine and then answered every call with
 * `{"error": "authentication required"}`.  A missing secret presented as a
 * mysteriously broken Computer tab, which is a genuinely bad way to find out.
 *
 * Naming the variable is the whole point.  Whoever hit this should not have to
 * read the backend to learn that the deployment was never finished.
 */
function NoPassphraseScreen() {
  const check = useAuth((s) => s.check);
  const [retrying, setRetrying] = useState(false);

  // Re-check rather than reload: once the token is set and the backend is
  // restarted, this turns into the normal login screen in place.
  const retry = async () => {
    setRetrying(true);
    try {
      await check();
    } finally {
      setRetrying(false);
    }
  };

  return (
    <div className="login">
      <div className="login__card">
        <div className="login__orb" />
        <ShieldAlert size={26} />
        <h1 className="login__title">Server not configured</h1>
        <p className="login__sub">
          This deployment has no <code>RAG_AUTH_TOKEN</code> set, so the backend is
          refusing every request rather than serving a signed-in browser to the open
          internet. That is the intended safe behaviour, not a bug.
        </p>
        <p className="login__sub">
          Set the token, then restart the backend:
        </p>
        <pre className="login__code">
          {`printf 'RAG_AUTH_TOKEN=%s\\n' 'your-passphrase' >> backend/.env\nbash codespace/boot.sh`}
        </pre>
        <button className="btn" onClick={() => void retry()} disabled={retrying}>
          {retrying ? <Loader2 size={15} className="spin" /> : <RefreshCw size={15} />}
          Check again
        </button>
      </div>
    </div>
  );
}

/**
 * Decides between the app and the lock screen.
 *
 * `auth_required` comes from the server.  It is false only when no passphrase is
 * configured at all, and that is a broken deployment rather than an open one --
 * see NoPassphraseScreen -- so it gets its own screen instead of either the app
 * (which cannot make a single successful request) or a login form that nobody
 * could ever pass.
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

  if (!required) return <NoPassphraseScreen />;
  if (!authenticated) return <LoginScreen />;
  if (status === "offline") return <LoginScreen />;
  return <>{children}</>;
}
