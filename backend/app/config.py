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


def _screen_ws_url() -> str:
    """Where the live screen lives, as a WebSocket URL.

    The app and the screen are served from one hostname, so the Access cookie
    the browser already holds for the app also authorises the screen.  Deriving
    the URL from COMPUTER_HOSTNAME keeps the domain in a single setting: the
    tunnel config and the viewer can never drift apart.
    """
    explicit = _get("COMPUTER_WS_URL", "").strip()
    if explicit:
        return explicit
    host = _get("COMPUTER_HOSTNAME", "").strip()
    if not host:
        # No tunnel configured: the viewer falls back to this backend's own
        # relay at /ws/screen, which is a development convenience only.
        return ""
    if host.startswith(("http://", "https://")):
        return "wss://" + host.split("://", 1)[1].rstrip("/") + "/websockify"
    return f"wss://{host.rstrip('/')}/websockify"


@dataclass
class Settings:
    host: str = field(default_factory=lambda: _get("HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: int(_get("PORT", "8000")))
    data_dir: str = field(default_factory=lambda: _get("DATA_DIR", "./data"))

    commander_provider: str = field(default_factory=lambda: _get("COMMANDER_PROVIDER", "mock"))
    commander_model: str = field(default_factory=lambda: _get("COMMANDER_MODEL", "claude-sonnet-4"))
    worker_provider: str = field(default_factory=lambda: _get("WORKER_PROVIDER", "mock"))
    worker_model: str = field(default_factory=lambda: _get("WORKER_MODEL", "gpt-4o"))
    browser_provider: str = field(default_factory=lambda: _get("BROWSER_PROVIDER", "mock"))
    browser_model: str = field(default_factory=lambda: _get("BROWSER_MODEL", "gemini-2.5-pro"))

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
    # websockify turns that into a WebSocket on 127.0.0.1:6080.  Neither is
    # published; the only external route is the Cloudflare Tunnel.
    screen_vnc_enabled: bool = field(default_factory=lambda: _get_bool("SCREEN_VNC_ENABLED", "1"))
    screen_vnc_port: int = field(default_factory=lambda: int(_get("SCREEN_VNC_PORT", "5900")))
    screen_ws_port: int = field(default_factory=lambda: int(_get("SCREEN_WS_PORT", "6080")))

    # The public, Access-protected WebSocket the Computer view connects to.
    # Usually derived from COMPUTER_HOSTNAME so there is exactly one place to
    # set the domain: set the hostname once, and the app, the tunnel config and
    # the viewer URL all agree.  Override COMPUTER_WS_URL only if the screen
    # really lives somewhere else.
    computer_ws_url: str = field(default_factory=lambda: _screen_ws_url())

    # The Vite build to serve, so the app and the screen share one origin.
    frontend_dist: str = field(default_factory=lambda: _get("FRONTEND_DIST", ""))

    max_text_chars: int = field(default_factory=lambda: int(_get("MAX_TEXT_CHARS", "8000")))
    max_web_chars: int = field(default_factory=lambda: int(_get("MAX_WEB_CHARS", "12000")))
    max_iters: int = field(default_factory=lambda: int(_get("MAX_ITERS", "4")))


def load_settings() -> Settings:
    return Settings()