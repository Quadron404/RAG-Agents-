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
# Serves the built UI and proxies /websockify to the local websockify, so the
# whole product is one origin.  Same host for the page and the WebSocket is what
# lets the session cookie ride along with the upgrade without any extra work.
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
# The tunnel is started by the supervisor, so give it a moment to be handed a
# hostname before reporting on it.
sleep 8
x_state="down";  [ -S "/tmp/.X11-unix/X${DESKTOP_DISPLAY#:}" ] && x_state="up"
chrome_state="down"; listening "$CHROME_DEBUG_PORT" && chrome_state="up"
vnc_state="down"; listening "$VNC_PORT" && vnc_state="up"
ws_state="down";  listening "$WEBSOCKIFY_PORT" && ws_state="up"
api_state="down"; listening "$BACKEND_PORT" && api_state="up"

log "======================================"
log " display   $x_state   ($DESKTOP_SIZE on $DESKTOP_DISPLAY)"
log " chrome    $chrome_state   profile: $CHROME_PROFILE"
log " RFB       $vnc_state   127.0.0.1:$VNC_PORT  (loopback only)"
log " noVNC     $ws_state   127.0.0.1:$WEBSOCKIFY_PORT  (loopback only)"
log " backend   $api_state   :$BACKEND_PORT"

if [ -s "$PUBLIC_URL_FILE" ]; then
  log " public    $(cat "$PUBLIC_URL_FILE")  (passphrase required)"
  log " screen    $(cat "$PUBLIC_URL_FILE")/websockify  (via the app, not exposed directly)"
else
  log " public    (tunnel not up yet; check $DESKTOP_LOG_DIR/cloudflared.log)"
  log "            until then the viewer falls back to the in-app /ws/screen relay)"
fi

if [ -z "${RAG_AUTH_TOKEN:-}" ] && [ -z "$(grep -s '^RAG_AUTH_TOKEN=' "$REPO_ROOT/backend/.env" 2>/dev/null | cut -d= -f2-)" ]; then
  log ""
  log " WARNING: RAG_AUTH_TOKEN is not set.  The app will refuse every route"
  log "          until it is, which is deliberate -- a public tunnel in front of"
  log "          a signed-in browser must never open by default.  Set it in"
  log "          backend/.env or as a Codespaces secret, then restart the backend."
fi

log "======================================"
log " local noVNC check:  http://127.0.0.1:$WEBSOCKIFY_PORT/vnc.html"
log " logs: $DESKTOP_LOG_DIR/{supervisor,agent,cloudflared,backend}.log"
