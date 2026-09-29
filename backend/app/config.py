import os
from dataclasses import dataclass, field
from pathlib import Path


def _load_dotenv() -> None:
    """Populate os.environ from backend/.env without overriding real env vars.

    Settings are read straight from the environment, so without this a checked
    in .env would be silently ignored.
    """
    env_path = Path(__file__).resolve().parents[1] / ".env"
    if not env_path.is_file():
        return
    try:
        text = env_path.read_text(encoding="utf-8")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv()


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