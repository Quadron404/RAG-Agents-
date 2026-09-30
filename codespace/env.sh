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
# What protects the screen is therefore NOT the URL as such -- a trycloudflare
# hostname turns up in DNS and proxy logs, and anyone who sees it can call the
# same origin.  The app used to add a passphrase (RAG_AUTH_TOKEN) on top; that
# was removed at the owner's request, so the URL is now the credential and the
# only way to revoke access is to restart the tunnel for a new one.  The
# remaining boundary is the loopback bind below: only the app is ever published.
export CLOUDFLARED_BIN="${CLOUDFLARED_BIN:-cloudflared}"
# Only the app is published.  5900 and 6080 stay on loopback forever.
export TUNNEL_ORIGIN="http://127.0.0.1:$BACKEND_PORT"
# The tunnel script writes the live origin here; /screen/config reads it so the
# browser learns the current hostname instead of guessing or being rebuilt.
export PUBLIC_URL_FILE="${PUBLIC_URL_FILE:-$DESKTOP_RUN_DIR/public-url}"

# --- helpers ---------------------------------------------------------------
log() { printf '[computer] %s\n' "$*" >&2; }

# The Codespace runtime user.
#
# Normally whoever is running the script.  If a setup step was ever invoked
# through sudo, the workspace's own owner is the real runtime user, so the run
# dir belongs to them and not to root.
codespace_user() {
  local u
  u="$(stat -c %U "$REPO_ROOT" 2>/dev/null || true)"
  if [ -n "$u" ] && [ "$u" != "root" ]; then
    printf '%s\n' "$u"
  else
    printf '%s\n' "$(id -un)"
  fi
}

# Make sure the run dir exists and belongs to the runtime user.
#
# `mkdir -p` is the entire reason this kept coming back.  It succeeds silently
# when the directory already exists, so a /tmp/ragdesktop left behind by an
# earlier root run keeps its owner forever, and then every write from the
# unprivileged stack fails:
#
#   boot.sh: line 74: /tmp/ragdesktop/supervisor.pid: Permission denied
#
# The supervisor never starts, so the backend never starts, so the Quick Tunnel
# is never asked for a hostname and there is no public URL at all.  Ownership is
# therefore checked explicitly and repaired when -- and only when -- it is
# genuinely wrong, which leaves a healthy directory completely alone on every
# later boot.  That is what makes a second run of boot.sh a no-op instead of a
# second chance to get the owner wrong.
#
# Repaired with chown to the runtime user, never with chmod 777.  The stack runs
# unprivileged, and 777 would hand write access to every account on the machine
# for a directory whose pid files decide which processes get killed.
ensure_run_dir() {
  local dir="${1:-$DESKTOP_RUN_DIR}"
  local user
  user="$(codespace_user)"
  # -p so a missing parent is created too, and an existing directory is left as
  # it is -- that existing-but-wrong-owner case is the one handled below.
  mkdir -p "$dir" 2>/dev/null || true

  if [ ! -d "$dir" ]; then
    log "FATAL: $dir does not exist and could not be created"
    return 1
  fi
  # Ours and writable: the common case, and deliberately untouched.
  if [ -O "$dir" ] && [ -w "$dir" ]; then
    return 0
  fi

  # Ours but the owner bit was lost (a stripped mode, a restore from a tarball).
  # No privilege is involved: the owner bit is ours to set, so this must not
  # depend on sudo being present.
  if [ -O "$dir" ]; then
    chmod u+rwx "$dir" 2>/dev/null
    if [ ! -w "$dir" ]; then
      log "FATAL: $dir is owned by $user but is still not writable"
      return 1
    fi
    log "$dir is now writable by $user (mode $(stat -c %a "$dir"))"
    return 0
  fi

  # Somebody else's directory, which is the reported failure: install.sh made
  # this one as root.  Fixing an owner needs privilege, so this is the only
  # place in the whole stack where sudo appears.
  local owner
  owner="$(stat -c %U "$dir" 2>/dev/null || echo unknown)"
  log "$dir is owned by $owner, not $user; repairing"
  if [ "$(id -u)" = 0 ]; then
    chown -R "$user" "$dir" 2>/dev/null
    chmod u+rwx "$dir" 2>/dev/null
  elif command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
    # -n, never a prompt: a Codespace cannot answer one, and a boot blocked on a
    # password request is worse than a boot that prints what to run.
    sudo -n chown -R "$user" "$dir" 2>/dev/null
    sudo -n chmod u+rwx "$dir" 2>/dev/null
  else
    log "FATAL: $dir is owned by $owner and cannot be repaired without sudo.  Run:"
    log "         sudo chown -R $user $dir"
    return 1
  fi

  if [ ! -w "$dir" ]; then
    log "FATAL: $dir is still not writable by $user after the repair"
    return 1
  fi
  log "$dir is now owned by $user (mode $(stat -c %a "$dir"))"
  return 0
}

# Write a pid file, replacing one we are not allowed to overwrite.
#
# Directory ownership normally decides this, and a pid file is only ever read to
# decide whether a process is still alive -- so one that survived a root run is
# stale by definition and safe to remove, and removable because the directory is
# ours.  Without this the write fails silently and the caller goes on believing
# it has a pid file for a process it cannot find again.
write_pid() {
  local file="$1" pid="$2"
  if [ -e "$file" ] && [ ! -w "$file" ]; then
    log "replacing stale $file left by another user"
    rm -f "$file" 2>/dev/null || true
  fi
  if ! printf '%s\n' "$pid" > "$file" 2>/dev/null; then
    log "FATAL: could not write $file"
    return 1
  fi
  return 0
}

listening() {
  # A plain TCP connect is the honest test: it is exactly what a viewer does.
  (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null && exec 3<&- && return 0
  return 1
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || { log "FATAL: $1 is not installed"; return 1; }
}
