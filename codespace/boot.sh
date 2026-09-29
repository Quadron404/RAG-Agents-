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
    # Fatal, not a warning.  With no dist the app falls back to the bundled
    # prototype in backend/app/static, which answers every request perfectly --
    # it is just a stale page with no login screen, no Computer view and no VNC.
    # A warning here is how a failed build reached someone as a working app.
    log "FATAL: the frontend build failed.  The app would serve the fallback"
    log "       prototype, which has no login screen and no screen view."
    log "       last lines of $DESKTOP_LOG_DIR/frontend-build.log:"
    tail -n 20 "$DESKTOP_LOG_DIR/frontend-build.log" 2>/dev/null | while read -r l; do log "         $l"; done
    exit 1
  fi
else
  log "frontend already built"
fi

# --- 2) the screen stack (Xvfb, Chrome, x11vnc, websockify, tunnel) ---------
# The agent daemon owns Xvfb/wm/x11vnc/websockify/Chrome, so starting it is
# what brings the screen up.  The tunnel is started by the supervisor after
# that, and it waits for the app on :8000 before asking for a hostname.
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
# keeps it one origin with no CORS or separate-tunnel handling.
if [ -f "$RUN_DIR/backend.pid" ] && kill -0 "$(cat "$RUN_DIR/backend.pid" 2>/dev/null)" 2>/dev/null; then
  # Restart rather than leave it alone, so "edit backend/.env and re-run boot.sh"
  # actually produces a process that has read the new file.  The backend is
  # stateless, so a restart costs one dropped request.
  OLD_PID="$(cat "$RUN_DIR/backend.pid" 2>/dev/null)"
  log "backend already running (pid $OLD_PID); restarting to pick up current configuration"
  kill "$OLD_PID" 2>/dev/null
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    kill -0 "$OLD_PID" 2>/dev/null || break
    sleep 0.3
  done
  kill -9 "$OLD_PID" 2>/dev/null
  rm -f "$RUN_DIR/backend.pid"
fi

# Prefer the project venv.  A previous version of this script ran a bare
# `python3 -m uvicorn`, which died with ModuleNotFoundError on a Codespace
# where install.sh had installed into the venv, and the symptom was simply
# "the app is not up" with the reason buried in a log nobody was reading.
BACKEND_PY=""
for cand in "$REPO_ROOT/backend/.venv/bin/python" "$(command -v python3 || true)"; do
  if [ -n "$cand" ] && [ -x "$cand" ]; then
    if "$cand" -c "import uvicorn, fastapi" >/dev/null 2>&1; then
      BACKEND_PY="$cand"
      break
    fi
  fi
done

if [ -z "$BACKEND_PY" ]; then
  log "FATAL: no python with uvicorn+fastapi.  Run codespace/install.sh first."
  log "       tried: backend/.venv/bin/python, $(command -v python3 || echo 'python3')"
else
  log "using $BACKEND_PY"
  # 127.0.0.1, never 0.0.0.0.  The Quick Tunnel reaches this port from an
  # outbound connection, so a public bind is never needed -- and binding
  # 0.0.0.0 would put the app, and therefore /websockify, on every interface
  # the Codespace has.  cloudflared, the app, and this process are all on the
  # same machine; loopback is the whole route.
  (
    cd "$REPO_ROOT/backend" || exit 1
    # backend/.env is deliberately NOT sourced here.
    #
    # It used to be: `set -a; . ./.env; set +a`, which made *bash* a second
    # parser of the same file.  Bash and config.py disagreed about quotes,
    # unquoted spaces, `$` expansion, and which of several duplicate lines won,
    # because bash takes the last and Python takes the first -- so the running
    # server could use a value that appeared nowhere in the file the operator was
    # reading.
    #
    # The backend loads the file itself, once, in config._load_dotenv.
    # Codespaces secrets still reach the process the normal way: `exec` inherits
    # this shell's environment, and the real environment still wins over the
    # file.  Only the second, competing parser is gone.
    exec "$BACKEND_PY" -m uvicorn app.main:app --host 127.0.0.1 --port "$BACKEND_PORT"
  ) >>"$DESKTOP_LOG_DIR/backend.log" 2>&1 &
  echo $! > "$RUN_DIR/backend.pid"
  log "backend started (pid $!) on 127.0.0.1:$BACKEND_PORT"

  # Wait for it and, if it never arrives, say why instead of leaving the
  # operator to infer it from a later "not listening" check.
  for _ in $(seq 1 30); do
    listening "$BACKEND_PORT" && break
    kill -0 "$(cat "$RUN_DIR/backend.pid")" 2>/dev/null || break
    sleep 1
  done
  if ! listening "$BACKEND_PORT"; then
    log "WARNING: the app never came up on 127.0.0.1:$BACKEND_PORT."
    log "         last lines of $DESKTOP_LOG_DIR/backend.log:"
    tail -n 15 "$DESKTOP_LOG_DIR/backend.log" 2>/dev/null | while read -r l; do log "         $l"; done
  fi
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
  log " public    $(cat "$PUBLIC_URL_FILE")  (no sign-in)"
  log " screen    $(cat "$PUBLIC_URL_FILE")/websockify  (via the app, not exposed directly)"
else
  log " public    (tunnel not up yet; check $DESKTOP_LOG_DIR/cloudflared.log)"
  log "            until then the viewer falls back to the in-app /ws/screen relay)"
fi

if [ -s "$PUBLIC_URL_FILE" ]; then
  log ""
  log " NOTE: this URL has no passphrase. Anyone who has it has the app, the"
  log "       files and the screen. Restart the tunnel to invalidate it."
fi

log "======================================"
log " local noVNC check:  http://127.0.0.1:$WEBSOCKIFY_PORT/vnc.html"
log " logs: $DESKTOP_LOG_DIR/{supervisor,agent,cloudflared,backend}.log"
