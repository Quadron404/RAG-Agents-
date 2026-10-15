import os
from dataclasses import dataclass, field
from pathlib import Path


def _load_dotenv() -> None:
    """Populate os.environ from backend/.env.

    Settings are read straight from the environment, so without this a .env in
    the repo would be silently ignored.  This is the only parser for that file:
    boot.sh deliberately does not source it, because bash and Python disagreed
    about quotes, spaces, ``$`` expansion and which of several duplicate lines
    won, and the loser of that disagreement was a value the operator could not
    see in the file they were reading.

    Two rules: a real environment variable wins over the file (that is how a
    Codespaces secret beats a file someone edited), and within the file the
    first definition of a key wins.
    """
    path = Path(__file__).resolve().parents[1] / ".env"
    if not path.is_file():
        return
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return

    seen: set[str] = set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not key or key in seen or key in os.environ:
            continue
        seen.add(key)
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

    # --- Mistral, the second computer-control provider ---------------------
    # A peer of OpenRouter, not a special case: the same loop, the same prompt,
    # the same parser and the same executors, with a different name and a
    # different model behind the provider interface.  Registering it here rather
    # than as another field per role is what keeps one implementation of the
    # loop instead of two.
    #
    # The model defaults to mistral-small-2506 rather than to empty, because a
    # configured key with a blank model is a typo rather than an intention, and
    # failing the run on a missing model name is a worse answer than trying the
    # small vision model the key is presumably for.
    mistral_api_key: str = field(default_factory=lambda: _get("MISTRAL_API_KEY", "").strip())
    mistral_model: str = field(default_factory=lambda: _get("MISTRAL_MODEL", "mistral-small-2506").strip())
    mistral_base_url: str = field(
        default_factory=lambda: _get("MISTRAL_BASE_URL", "https://api.mistral.ai/v1")
    )

    # --- Groq, the third computer-control provider -------------------------
    # Same shape as the two above, and for the same reason: the loop, the
    # prompt, the parser and the executors live above the provider interface,
    # so a provider is a name, a key and a model rather than a second control
    # loop.  The key is a Codespaces secret read from the environment and only
    # ever used to build the Authorization header inside the adapter.
    #
    # The model is named rather than blank, because computer control without a
    # vision model is not computer control: with no model configured, Groq is
    # "not configured" and the run says so by name instead of quietly sending a
    # screenshot to a text-only model.
    groq_api_key: str = field(default_factory=lambda: _get("GROQ_API_KEY", "").strip())
    groq_model: str = field(default_factory=lambda: _get("GROQ_MODEL", "qwen/qwen3.8-27b").strip())
    groq_base_url: str = field(default_factory=lambda: _get("GROQ_BASE_URL", "https://api.groq.com/openai/v1"))

    # --- Ordinary agent roles (commander / worker / email / summaries) ----
    # These are text roles and are deliberately separate from the computer
    # controller's GROQ_MODEL above, which stays on its vision model.  The
    # default is the GPT-OSS 120B model the platform targets, with a smaller
    # fallback available when a request does not need the larger one.
    agent_provider: str = field(default_factory=lambda: _get("AGENT_PROVIDER", "groq"))
    agent_model: str = field(default_factory=lambda: _get("AGENT_MODEL", "openai/gpt-oss-120b"))
    agent_fallback_model: str = field(default_factory=lambda: _get("AGENT_FALLBACK_MODEL", "openai/gpt-oss-20b"))
    agent_max_completion_tokens: int = field(
        default_factory=lambda: int(_get("AGENT_MAX_COMPLETION_TOKENS", "2048"))
    )
    agent_reasoning_effort: str = field(default_factory=lambda: _get("AGENT_REASONING_EFFORT", "low"))

    # --- Authentication and the internal gateway --------------------------
    # The browser never sees these.  GOOGLE_CLIENT_SECRET, APP_ENCRYPTION_KEY
    # and RAG_INTERNAL_TOKEN are all runtime secrets supplied by Wrangler (the
    # Worker) or the Codespace environment; none is ever written to a file in
    # the repository.
    google_client_id: str = field(default_factory=lambda: _get("GOOGLE_CLIENT_ID", "").strip())
    google_client_secret: str = field(default_factory=lambda: _get("GOOGLE_CLIENT_SECRET", "").strip())
    google_auth_url: str = field(
        default_factory=lambda: _get("GOOGLE_AUTH_URL", "https://accounts.google.com/o/oauth2/v2/auth")
    )
    google_token_url: str = field(
        default_factory=lambda: _get("GOOGLE_TOKEN_URL", "https://oauth2.googleapis.com/token")
    )
    google_jwks_url: str = field(
        default_factory=lambda: _get("GOOGLE_JWKS_URL", "https://www.googleapis.com/oauth2/v3/certs")
    )
    google_userinfo_url: str = field(
        default_factory=lambda: _get("GOOGLE_USERINFO_URL", "https://openidconnect.googleapis.com/v1/userinfo")
    )
    #: Google's token-revocation endpoint.  Called best-effort on disconnect so
    #: the granted refresh token is actually invalidated at Google rather than
    #: only forgotten locally.  The token is sent in the body and never logged.
    google_revoke_url: str = field(
        default_factory=lambda: _get("GOOGLE_REVOKE_URL", "https://oauth2.googleapis.com/revoke")
    )
    #: How long before a Gmail access token's stated expiry it is refreshed, so a
    #: token is never handed out moments before it stops working.
    gmail_refresh_skew_seconds: int = field(
        default_factory=lambda: int(_get("GMAIL_REFRESH_SKEW_SECONDS", "60"))
    )

    # --- Bot sender for owner-only notifications -----------------------------
    # Dedicated bot-owned sender mailbox (never the owner's Gmail). Preferred:
    # if sending with Gmail API, use bot_refresh_token; if SMTP, use SMTP creds.
    bot_sender_provider: str = field(
        default_factory=lambda: _get("BOT_SENDER_PROVIDER", "").strip().lower()
    )  # gmail|smtp
    bot_sender_email: str = field(
        default_factory=lambda: _get("BOT_SENDER_EMAIL", "").strip()
    )
    bot_sender_refresh_token: str = field(
        default_factory=lambda: _get("BOT_SENDER_REFRESH_TOKEN", "").strip()
    )
    # Gmail API uses same client as OAuth for token exchange? We reuse google_token_url/ids
    bot_sender_smtp_host: str = field(
        default_factory=lambda: _get("BOT_SMTP_HOST", "").strip()
    )
    bot_sender_smtp_port: int = field(
        default_factory=lambda: int(_get("BOT_SMTP_PORT", "587") or 587)
    )
    bot_sender_smtp_user: str = field(
        default_factory=lambda: _get("BOT_SMTP_USER", "").strip()
    )
    bot_sender_smtp_password: str = field(
        default_factory=lambda: _get("BOT_SMTP_PASSWORD", "").strip()
    )
    bot_sender_smtp_use_tls: bool = field(
        default_factory=lambda: _get_bool("BOT_SMTP_USE_TLS", "1")
    )
    bot_sender_smtp_use_ssl: bool = field(
        default_factory=lambda: _get_bool("BOT_SMTP_USE_SSL", "0")
    )
    #: The stable public origin the Worker serves at.  Used only to build the
    #: OAuth redirect URI, which must match what Google has registered
    #: byte-for-byte.  Empty means "derive from the incoming request".
    public_base_url: str = field(default_factory=lambda: _get("PUBLIC_BASE_URL", "").rstrip("/"))
    #: The Cloudflare Worker's origin.  Routines live at the edge (D1), because a
    #: schedule has to keep running while this backend is stopped, so the routine
    #: tools are clients of the Worker rather than of a local table.  Empty means
    #: "use PUBLIC_BASE_URL", which is the same origin the browser reaches.
    worker_origin: str = field(default_factory=lambda: _get("WORKER_ORIGIN", "").rstrip("/"))
    #: Secret shared only between the Cloudflare Worker and this backend.  When
    #: set, every protected route requires it (or a same-origin session), which
    #: is what makes a random Quick Tunnel URL insufficient to reach the API.
    rag_internal_token: str = field(default_factory=lambda: _get("RAG_INTERNAL_TOKEN", "").strip())
    app_encryption_key: str = field(default_factory=lambda: _get("APP_ENCRYPTION_KEY", "").strip())
    #: HMAC key for session identifiers.  Falls back to the encryption key so a
    #: single configured secret still hashes sessions; a dedicated value is
    #: preferred so rotating one does not rotate both.
    session_secret: str = field(default_factory=lambda: _get("SESSION_SECRET", "").strip())
    session_ttl_seconds: int = field(default_factory=lambda: int(_get("SESSION_TTL_SECONDS", str(60 * 60 * 24 * 30))))

    # --- Passive summaries ------------------------------------------------
    # A conversation is summarised after this many seconds without activity.
    summary_idle_seconds: int = field(default_factory=lambda: int(_get("SUMMARY_IDLE_SECONDS", "600")))
    history_token_budget: int = field(default_factory=lambda: int(_get("HISTORY_TOKEN_BUDGET", "3000")))

    # --- Restricted browsing for ordinary agents --------------------------
    # Ordinary agents must not share the computer-control browser or the
    # personal Chrome profile, and their web access must pass a genuine,
    # network-level allowlist instead of a URL string check.  When this is on,
    # the network tools (browse / fetch_url / web_search / shell) are routed
    # through the restricted runtime and fail closed when it is not available;
    # the one unrestricted browser tool is removed from the ordinary-agent
    # registry entirely, so there is no second door.  Off is the default only so
    # an un-configured checkout keeps working; the Codespace launcher turns it
    # on.  Turning it on with an empty allowlist allows nothing, which is the
    # intended default-deny.
    restricted_enabled: bool = field(default_factory=lambda: _get_bool("RESTRICTED_BROWSING", "0"))
    # The owner-controlled allowlist.  /etc/rag-restricted/allowlist is the
    # deployed path precisely because it is root-owned and mode 0644 on the
    # Codespace: the unprivileged ordinary agent can read it and cannot write
    # it.  The repo file is only a fallback for a developer machine, and is
    # never the source of truth once the deployed path exists.
    restricted_allowlist_path: str = field(
        default_factory=lambda: _get("RESTRICTED_ALLOWLIST_PATH", "").strip()
    )
    # The allowlisting forward proxy.  The isolated browser and the fetch tool
    # can only leave through this socket, and the sandbox's firewall drops
    # everything else, so a browser that ignores its own URL check still cannot
    # reach a destination the proxy refuses.
    restricted_gateway_url: str = field(
        default_factory=lambda: _get("RESTRICTED_GATEWAY_URL", "http://127.0.0.1:9111").rstrip("/")
    )
    # The separate browser service.  Its own profile directory and its own
    # debug port, neither of which is the computer-control browser's.
    restricted_browser_url: str = field(
        default_factory=lambda: _get("RESTRICTED_BROWSER_URL", "http://127.0.0.1:9110").rstrip("/")
    )
    restricted_profile_dir: str = field(
        default_factory=lambda: _get("RESTRICTED_PROFILE_DIR", "/tmp/rag-restricted-profile")
    )
    # The shell that runs the ordinary agent's commands.  It must live inside
    # the same network sandbox as the browser, or a single `curl` walks around
    # the gateway.  Empty means "not deployed", and the shell tool then refuses
    # rather than silently running on the unrestricted host.
    restricted_shell_url: str = field(
        default_factory=lambda: _get("RESTRICTED_SHELL_URL", "").rstrip("/")
    )

    # The loop's own limits, all deliberately small: a control loop that can run
    # a hundred clicks is a control loop that can empty someone's wallet or
    # navigate somewhere it should not.
    computer_max_steps: int = field(default_factory=lambda: int(_get("COMPUTER_MAX_STEPS", "12")))
    computer_max_json_retries: int = field(default_factory=lambda: int(_get("COMPUTER_MAX_JSON_RETRIES", "2")))
    # Total HTTP requests allowed per model call, the first one included.
    # Small on purpose: these are re-sends against a quota that is already
    # exhausted, and a large number turns one task into a burst that keeps the
    # limit closed.  Named "attempts" rather than "retries" because 3 means 3
    # requests, not 1 plus 3 -- the loop counted attempts, and the old name
    # implied a budget four times larger than the one actually enforced.
    computer_max_http_attempts: int = field(default_factory=lambda: int(_get("COMPUTER_MAX_HTTP_ATTEMPTS", "3")))
    computer_retry_base_seconds: float = field(
        default_factory=lambda: float(_get("COMPUTER_RETRY_BASE_SECONDS", "1.5"))
    )
    #: Ceiling for a single computed backoff, so a long outage cannot turn one
    #: request into a request that sleeps for an hour.
    computer_retry_max_seconds: float = field(
        default_factory=lambda: float(_get("COMPUTER_RETRY_MAX_SECONDS", "30"))
    )
    computer_settle_ms: int = field(default_factory=lambda: int(_get("COMPUTER_SETTLE_MS", "1400")))
    computer_settle_ms_click: int = field(default_factory=lambda: int(_get("COMPUTER_SETTLE_MS_CLICK", "0")))
    #: Budget (ms) a state-changing action may take to stop changing the page,
    #: measured by polling the page until two consecutive reads agree.  The two
    #: legacy settles above time "how long to wait"; this one times "whether the
    #: page is still", which is the question a state read afterwards actually
    #: answers.
    computer_state_settle_ms: int = field(default_factory=lambda: int(_get("COMPUTER_STATE_SETTLE_MS", "600")))

    # --- How much a single model call is allowed to say ------------------------
    # Computer control is a loop of one-tool decisions, so the only thing a
    # call has to return is `{"name": ..., "arguments": {...}}`.  A thousand
    # tokens of budget for that is two orders of magnitude more than the work
    # needs, and on a reasoning model the unspent budget is not free: the model
    # is free to spend it thinking out loud, which is the single largest
    # avoidable cost in a loop that makes forty calls.
    computer_max_completion_tokens: int = field(
        default_factory=lambda: int(_get("COMPUTER_MAX_COMPLETION_TOKENS", "256"))
    )
    #: "none" is a request, not a preference: it tells the provider not to emit
    #: a reasoning prefix.  Some models spend more tokens on thinking about a
    #: click than on everything else the run does.  Empty by default rather than
    #: "none" because the field is rejected outright by endpoints that do not
    #: implement it, and only Groq is relied upon to; Mistral and OpenRouter are
    #: left exactly as they were unless an operator opts in.
    computer_reasoning_effort: str = field(default_factory=lambda: _get("COMPUTER_REASONING_EFFORT", ""))
    #: How many history lines ride along on each request.  The point of the
    #: history is to replace the transcript, and a transcript that runs away in
    #: length is the thing being replaced.  Twenty lines is roughly a screen of
    #: text, which is more than a short task needs and far less than a long one
    #: can produce, so it is a ceiling rather than a target.
    computer_max_history_lines: int = field(
        default_factory=lambda: int(_get("COMPUTER_MAX_HISTORY_LINES", "20"))
    )

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

    max_text_chars: int = field(default_factory=lambda: int(_get("MAX_TEXT_CHARS", "8000")))
    max_web_chars: int = field(default_factory=lambda: int(_get("MAX_WEB_CHARS", "12000")))
    max_iters: int = field(default_factory=lambda: int(_get("MAX_ITERS", "4")))


def load_settings() -> Settings:
    return Settings()