#!/usr/bin/env bash
# Shared settings for the whole Computer stack.
#
# Sourced by start-computer.sh, start-tunnel.sh and supervise.sh so there is
# exactly one place that decides which display, ports and profile are in use.
# Every value can be overridden from the environment, which is how the
# multi-user layout later gets one browser per user instead of one shared one.

# --- display ---------------------------------------------------------------
export DESKTOP_DISPLAY="${DESKTOP_DISPLAY:-:99}"
export DESKTOP_SIZE="${DESKTOP_SIZE:-1365x768}"
export DESKTOP_DEPTH="${DESKTOP_DEPTH:-24}"
export DESKTOP_WM="${DESKTOP_WM:-fluxbox}"
export DISPLAY="$DESKTOP_DISPLAY"

# --- browser ---------------------------------------------------------------
# The profile is the user's login state.  It has to sit on the persistent
# Codespaces volume, never on the container filesystem, or every rebuild would
# log the user out of every site.
export CHROME_PROFILE="${CHROME_PROFILE:-/workspaces/chrome-profile}"
export CHROME_START_URL="${CHROME_START_URL:-https://x.com}"
export CHROME_DEBUG_PORT="${CHROME_DEBUG_PORT:-9222}"

# --- ports (loopback only) -------------------------------------------------
# x11vnc exports the real framebuffer as RFB.  websockify turns that into a
# WebSocket and serves the noVNC client.  Both bind 127.0.0.1 and nothing else.
#
# The tunnel publishes neither of them.  It publishes the app on BACKEND_PORT,
# and the app proxies /websockify to 6080 after checking the session cookie, so
# RFB never has a public port of its own.
export VNC_PORT="${VNC_PORT:-5900}"
export WEBSOCKIFY_PORT="${WEBSOCKIFY_PORT:-6080}"
export NOVNC_WEB="${NOVNC_WEB:-/usr/share/novnc}"

# --- repo ------------------------------------------------------------------
CODESPACE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export REPO_ROOT="$(dirname "$CODESPACE_DIR")"
export AGENT_PY="$REPO_ROOT/backend/vm_agent/daemon.py"

# --- agent (the 3 apps: browser, files, terminal) -------------------------
export AGENT_PORT="${AGENT_PORT:-9000}"
# Derived from the checkout rather than guessed: the Codespaces folder is named
# after the repo, and a hardcoded "rag-agents" would silently point the agent at
# a directory that does not exist.
export WORKSPACE="${WORKSPACE:-$REPO_ROOT}"
export BACKEND_PORT="${BACKEND_PORT:-8000}"

# --- writable run/log dirs -------------------------------------------------
# A Codespace user is not root, so the Alpine-style /run and /var/log defaults
# are unusable.  The log dir stays /tmp so the browser log keeps the
# long-standing name /tmp/chromium.log.
export DESKTOP_RUN_DIR="${DESKTOP_RUN_DIR:-/tmp/ragdesktop}"
export DESKTOP_LOG_DIR="${DESKTOP_LOG_DIR:-/tmp}"

# --- cloudflare ------------------------------------------------------------
# A Quick Tunnel: no account, no domain, no token, no config file.  cloudflared
# prints a random https://<something>.trycloudflare.com origin on startup, which
# changes on every restart, so nothing can be hardcoded anywhere.
#
# What protects the screen is therefore NOT the URL -- a trycloudflare hostname
# turns up in DNS and proxy logs, and anyone who sees it can call the same
# origin.  What protects it is the app's own session: RAG_AUTH_TOKEN mints a
# cookie, and the app refuses /websockify without one.  The tunnel is only a
# pipe; the passphrase is the lock.
export CLOUDFLARED_BIN="${CLOUDFLARED_BIN:-cloudflared}"
# Only the app is published.  5900 and 6080 stay on loopback forever.
export TUNNEL_ORIGIN="http://127.0.0.1:$BACKEND_PORT"
# The tunnel script writes the live origin here; /screen/config reads it so the
# browser learns the current hostname instead of guessing or being rebuilt.
export PUBLIC_URL_FILE="${PUBLIC_URL_FILE:-$DESKTOP_RUN_DIR/public-url}"

# --- helpers ---------------------------------------------------------------
log() { printf '[computer] %s\n' "$*" >&2; }

listening() {
  # A plain TCP connect is the honest test: it is exactly what a viewer does.
  (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null && exec 3<&- && return 0
  return 1
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || { log "FATAL: $1 is not installed"; return 1; }
}
