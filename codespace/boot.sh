#!/usr/bin/env bash
# Called on every Codespace start (postStartCommand).
#
# Codespaces kill all processes when the machine idles, so everything has to be
# re-launched detached.  This is the one place that decides the order:
# the screen stack first, then the app that shows it.
set -uo pipefail

CODESPACE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$CODESPACE_DIR/env.sh"

RUN_DIR="$DESKTOP_RUN_DIR"
mkdir -p "$RUN_DIR" "$CHROME_PROFILE" "$WORKSPACE"

log "=== RAG Agents computer booting ==="

# --- 1) the UI, if it has not been built yet ------------------------------
# Before the backend starts, because the backend decides what to serve by
# looking for Frontend/dist/index.html at import time.  Start it first on a
# fresh clone and the mount is decided from a directory that does not exist
# yet, so the app keeps serving the old prototype for the whole session.
if [ ! -f "$REPO_ROOT/Frontend/dist/index.html" ]; then
  log "building the frontend (first run only)"
  if (cd "$REPO_ROOT/Frontend" && npm run build) >>"$DESKTOP_LOG_DIR/frontend-build.log" 2>&1; then
    log "frontend built"
  else
    log "WARNING: frontend build failed; see $DESKTOP_LOG_DIR/frontend-build.log"
  fi
else
  log "frontend already built"
fi

# --- 2) the screen stack (Xvfb, Chrome, x11vnc, websockify, tunnel) ---------
if [ -f "$RUN_DIR/supervisor.pid" ] && kill -0 "$(cat "$RUN_DIR/supervisor.pid" 2>/dev/null)" 2>/dev/null; then
  log "supervisor already running"
else
  setsid bash "$CODESPACE_DIR/supervise.sh" \
    >>"$DESKTOP_LOG_DIR/supervisor.log" 2>&1 &
  echo $! > "$RUN_DIR/supervisor.pid"
  log "supervisor started (pid $!)"
fi

# --- 3) the RAG Agents backend --------------------------------------------
# Serves the built UI and proxies the 3 apps to the agent, so the whole
# product is one origin: same host as the WebSocket, which is what lets the
# Cloudflare Access cookie ride along with it.
if [ -f "$RUN_DIR/backend.pid" ] && kill -0 "$(cat "$RUN_DIR/backend.pid" 2>/dev/null)" 2>/dev/null; then
  log "backend already running"
else
  (
    cd "$REPO_ROOT/backend" || exit 1
    set -a
    [ -f .env ] && . ./.env
    set +a
    exec python3 -m uvicorn app.main:app --host 0.0.0.0 --port "$BACKEND_PORT"
  ) >>"$DESKTOP_LOG_DIR/backend.log" 2>&1 &
  echo $! > "$RUN_DIR/backend.pid"
  log "backend started (pid $!) on :$BACKEND_PORT"
fi

# --- 4) status -------------------------------------------------------------
sleep 3
x_state="down";  [ -S "/tmp/.X11-unix/X${DESKTOP_DISPLAY#:}" ] && x_state="up"
chrome_state="down"; listening "$CHROME_DEBUG_PORT" && chrome_state="up"
vnc_state="down"; listening "$VNC_PORT" && vnc_state="up"
ws_state="down";  listening "$WEBSOCKIFY_PORT" && ws_state="up"
api_state="down"; listening "$BACKEND_PORT" && api_state="up"
tunnel_state="skipped"; [ -n "${CF_TUNNEL_TOKEN:-}" ] && tunnel_state="started"

log "======================================"
log " display   $x_state   ($DESKTOP_SIZE on $DESKTOP_DISPLAY)"
log " chrome    $chrome_state   profile: $CHROME_PROFILE"
log " RFB       $vnc_state   127.0.0.1:$VNC_PORT  (loopback only)"
log " noVNC     $ws_state   127.0.0.1:$WEBSOCKIFY_PORT  (loopback only)"
log " backend   $api_state   :$BACKEND_PORT"
log " tunnel    $tunnel_state"
if [ -n "${COMPUTER_HOSTNAME:-}" ]; then
  log " public    https://$COMPUTER_HOSTNAME  (Cloudflare Access required)"
  log " screen    https://$COMPUTER_HOSTNAME/websockify"
else
  log " public    (set COMPUTER_HOSTNAME to publish; viewer will use the relay)"
fi
log "======================================"
log " local noVNC check:  http://127.0.0.1:$WEBSOCKIFY_PORT/vnc.html"
log " logs: $DESKTOP_LOG_DIR/{supervisor,agent,cloudflared,backend}.log"
