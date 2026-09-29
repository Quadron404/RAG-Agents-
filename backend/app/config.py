import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path

# Where the passphrase came from, and whether a value exists at all.  This exists
# because a wrong passphrase and a passphrase that was never loaded look exactly
# the same from the login screen: both are "incorrect passphrase".  The one
# diagnostic that separates them is the source, so it is recorded here.
_ENV_SOURCE: dict[str, str] = {}
_ENV_DUPLICATES: dict[str, int] = {}
_ENV_SHADOWED: dict[str, bool] = {}


def env_path() -> Path:
    """The backend/.env this process reads, if it exists."""
    return Path(__file__).resolve().parents[1] / ".env"


def _fingerprint(value: str) -> str:
    """A short, non-reversible stand-in for a secret.

    SHA-256 truncated to 12 hex characters, so a diagnostic can prove "the value
    I typed is the value the server loaded" without ever putting the passphrase
    in a log line, a log file, or a response body.  It is a fingerprint, not an
    encoding: it does not decrypt, and it is not reversible by any means short of
    guessing the input, which is the same guessing the passphrase already
    requires.
    """
    if not value:
        return ""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _load_dotenv() -> None:
    """Populate os.environ from backend/.env, recording where each value came from.

    Settings are read straight from the environment, so without this a .env in
    the repo would be silently ignored.

    Three rules, and they are the whole contract:

    1. A real environment variable wins.  That is how a Codespaces secret beats a
       file someone edited, and it must keep working.
    2. Within the file, the *first* definition of a key wins.  A file with two
       RAG_AUTH_TOKEN lines is a normal state for a Codespace that has been
       reconfigured, because the documented way to set it is an append
       (``printf ... >> backend/.env``).  Silently taking the last one instead
       means the operator reads a value the server is not using.
    3. Nothing here is shell.  This file used to be parsed a second time by
       ``set -a; . ./.env`` in boot.sh, and bash disagreed with Python about
       quotes, spaces, ``$`` expansion and which duplicate won -- so the server
       compared against a value nobody could see in the file.  One parser now.

    Every key records its source, and every duplicate is counted, so a mismatch
    can be reported as "these two differ" rather than "wrong passphrase".
    """
    path = env_path()
    if not path.is_file():
        return
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return

    for key in _ENV_SOURCE:
        _ENV_SOURCE[key] = "environment"

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        # Quoted values lose their quotes; this matches how a shell would read
        # them and keeps "a b" and 'a b' equivalent to a b.
        value = value.strip().strip('"').strip("'")
        if not key:
            continue
        if key in _ENV_DUPLICATES:
            # A second definition of a key the file already set.  The first one
            # stands; this is recorded so the diagnostic can say so out loud.
            _ENV_DUPLICATES[key] += 1
            continue
        if key in os.environ:
            # The environment is ahead of the file, which is the intended
            # precedence for a Codespaces secret.  Remember the file's value too
            # so the two can be compared without printing either.
            _ENV_SOURCE[key] = "environment"
            _ENV_SHADOWED[key] = True
            _ENV_DUPLICATES.setdefault(key, 0)
            continue
        os.environ[key] = value
        _ENV_SOURCE[key] = "backend/.env"
        _ENV_DUPLICATES.setdefault(key, 0)


_load_dotenv()


def describe(key: str) -> dict:
    """A safe description of a configured secret. Never includes the value.

    Returns only whether a value is loaded, where it came from, a short
    SHA-256 fingerprint, and whether the file holds a *different* value that the
    environment is shadowing.  That is the difference between "you typed the
    wrong thing" and "the server is not using the line you are looking at".
    """
    value = os.environ.get(key, "")
    source = _ENV_SOURCE.get(key)
    if source is None:
        source = "environment" if key in os.environ else "unset"
    info = {
        "loaded": bool(value),
        "source": source,
        "fingerprint": _fingerprint(value),
    }
    if key in _ENV_DUPLICATES and _ENV_DUPLICATES[key]:
        info["duplicate_lines_in_file"] = _ENV_DUPLICATES[key]
    if _ENV_SHADOWED.get(key):
        # The environment won, so say that the file disagrees rather than
        # leaving the operator to wonder why editing .env changed nothing.
        info["shadowed_file_value"] = True
        info["note"] = (
            f"{key} is set in the process environment, so backend/.env is ignored "
            "for it.  Unset the environment variable, or set the secret to the "
            "value you want."
        )
    return info


def _get(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _get_bool(key: str, default: str = "0") -> bool:
    return _get(key, default).strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    # 127.0.0.1, never 0.0.0.0.  The app is published by a Cloudflare Quick
    # Tunnel, which dials in over loopback from the same machine, so a wildcard
    # bind is never required -- and it would put the app, and therefore
    # /websockify and the signed-in Chrome behind it, on every interface the
    # Codespace has.  HOST is still honoured for a container network you
    # control, but it must be set deliberately; the Codespace launcher passes
    # 127.0.0.1 on the command line, which wins over this value regardless.
    host: str = field(default_factory=lambda: _get("HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: int(_get("PORT", "8000")))
    data_dir: str = field(default_factory=lambda: _get("DATA_DIR", "./data"))

    commander_provider: str = field(default_factory=lambda: _get("COMMANDER_PROVIDER", "mock"))
    commander_model: str = field(default_factory=lambda: _get("COMMANDER_MODEL", "claude-sonnet-4"))
    worker_provider: str = field(default_factory=lambda: _get("WORKER_PROVIDER", "mock"))
    worker_model: str = field(default_factory=lambda: _get("WORKER_MODEL", "gpt-4o"))
    browser_provider: str = field(default_factory=lambda: _get("BROWSER_PROVIDER", "mock"))
    browser_model: str = field(default_factory=lambda: _get("BROWSER_MODEL", "gemini-2.5-pro"))

    # --- Computer control (the AI driving the real remote browser) ----------
    # A role of its own, separate from commander/worker/browser, because it has
    # its own provider, its own model and its own prompt: the computer-control
    # model is looking at screenshots of a screen, not at text.
    computer_provider: str = field(default_factory=lambda: _get("COMPUTER_PROVIDER", "openrouter"))
    computer_model: str = field(default_factory=lambda: _get("OPENROUTER_MODEL", ""))

    # The key never leaves the server.  It is read from the environment (a
    # Codespaces secret, or backend/.env) and is only ever used to build the
    # Authorization header inside the provider.  No route, no config endpoint
    # and no frontend bundle ever sees it.
    openrouter_api_key: str = field(default_factory=lambda: _get("OPENROUTER_API_KEY", "").strip())
    openrouter_base_url: str = field(
        default_factory=lambda: _get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    )

    # The loop's own limits, all deliberately small: a control loop that can run
    # a hundred clicks is a control loop that can empty someone's wallet or
    # navigate somewhere it should not.
    computer_max_steps: int = field(default_factory=lambda: int(_get("COMPUTER_MAX_STEPS", "12")))
    computer_max_json_retries: int = field(default_factory=lambda: int(_get("COMPUTER_MAX_JSON_RETRIES", "2")))
    computer_settle_ms: int = field(default_factory=lambda: int(_get("COMPUTER_SETTLE_MS", "1400")))
    computer_settle_ms_click: int = field(default_factory=lambda: int(_get("COMPUTER_SETTLE_MS_CLICK", "900")))

    # --- The remote computer (GitHub Codespace) ----------------------------
    # There is no hypervisor any more.  The "computer" is the Codespace
    # itself: a real Linux box running real Google Chrome on a real X display.
    #
    # The agent (backend/vm_agent/daemon.py) runs there on 127.0.0.1:9000 and
    # serves the three apps -- browser, files and terminal.  Everything the
    # backend does with that machine goes through it.
    workspace_base_url: str = field(
        default_factory=lambda: _get("WORKSPACE_BASE_URL", "http://127.0.0.1:9000").rstrip("/")
    )

    # --- Live screen (the real framebuffer over VNC) ------------------------
    # x11vnc exports the Codespace's X display as RFB on 127.0.0.1:5900 and
    # websockify turns that into a WebSocket on 127.0.0.1:6080.  Neither is ever
    # published.  The Cloudflare Quick Tunnel points at this app on :8000, and
    # /websockify relays the last hop to :6080 -- so the RFB stream and both
    # ports stay on loopback and the only door is a route that requires a
    # session cookie.
    screen_vnc_enabled: bool = field(default_factory=lambda: _get_bool("SCREEN_VNC_ENABLED", "1"))
    screen_vnc_port: int = field(default_factory=lambda: int(_get("SCREEN_VNC_PORT", "5900")))
    screen_ws_port: int = field(default_factory=lambda: int(_get("SCREEN_WS_PORT", "6080")))

    # Where codespace/start-tunnel.sh records the Quick Tunnel URL it was
    # assigned.  Read on every /screen/config call so a tunnel restart is picked
    # up without a redeploy.  /tmp is deliberate: it is scratch state, not
    # configuration, and must not end up in git.
    public_url_file: str = field(
        default_factory=lambda: _get("PUBLIC_URL_FILE", "/tmp/ragdesktop/public-url")
    )

    # The Vite build to serve, so the app and the screen are one origin and the
    # session cookie is first-party for the WebSocket upgrade.
    frontend_dist: str = field(default_factory=lambda: _get("FRONTEND_DIST", ""))

    # The passphrase that guards the app.  There is no default on purpose: with
    # none set, every route except /health and /auth/* refuses to serve, so a
    # forgotten secret fails closed instead of exposing a signed-in browser.
    auth_token: str = field(default_factory=lambda: _get("RAG_AUTH_TOKEN", "").strip())

    max_text_chars: int = field(default_factory=lambda: int(_get("MAX_TEXT_CHARS", "8000")))
    max_web_chars: int = field(default_factory=lambda: int(_get("MAX_WEB_CHARS", "12000")))
    max_iters: int = field(default_factory=lambda: int(_get("MAX_ITERS", "4")))


def load_settings() -> Settings:
    return Settings()