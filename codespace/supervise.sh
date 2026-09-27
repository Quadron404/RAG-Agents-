#!/usr/bin/env bash
# One long-lived process that keeps the whole Computer stack running.
#
#   agent daemon  -> owns Xvfb, fluxbox, x11vnc and Google Chrome
#   websockify    -> 127.0.0.1:6080, the noVNC client + the RFB WebSocket
#   cloudflared   -> the authenticated public route
#
# Codespaces stop every process when the machine idles, so this is restarted
# from postStartCommand / a detached launch.  Each child is checked on a timer
# and restarted if it died, because a screen that comes back half-alive is far
# more annoying to debug than one that restarts cleanly.
set -uo pipefail

CODESPACE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$CODESPACE_DIR/env.sh"

RUN_DIR="$DESKTOP_RUN_DIR"
mkdir -p "$RUN_DIR"
SUPERVISOR_LOG="$DESKTOP_LOG_DIR/supervisor.log"

log "supervisor starting (pid $$), logs -> $SUPERVISOR_LOG"

# Children are started detached and tracked by pid file, so the supervisor can
# survive its own terminal being closed.
declare -A CHILD_PID=()

start_agent() {
  [ -f "$AGENT_PY" ] || { log "FATAL: agent not found at $AGENT_PY"; return 1; }
  WORKSPACE="$WORKSPACE" \
  DESKTOP_DISPLAY="$DESKTOP_DISPLAY" \
  DESKTOP_SIZE="$DESKTOP_SIZE" \
  DESKTOP_DEPTH="$DESKTOP_DEPTH" \
  DESKTOP_WM="$DESKTOP_WM" \
  VNC_PORT="$VNC_PORT" \
  CHROME_PROFILE="$CHROME_PROFILE" \
  CHROME_START_URL="$CHROME_START_URL" \
  CHROME_DEBUG_PORT="$CHROME_DEBUG_PORT" \
  DESKTOP_RUN_DIR="$DESKTOP_RUN_DIR" \
  DESKTOP_LOG_DIR="$DESKTOP_LOG_DIR" \
  setsid python3 "$AGENT_PY" "$AGENT_PORT" \
    >>"$DESKTOP_LOG_DIR/agent.log" 2>&1 &
  CHILD_PID[agent]=$!
  echo "$CHILD_PID[agent]" > "$RUN_DIR/supervisor-agent.pid"
  log "agent started (pid ${CHILD_PID[agent]})"
}

start_websockify() {
  # Already up (e.g. started by hand) means nothing to do.
  if listening "$WEBSOCKIFY_PORT"; then
    log "websockify already listening on 127.0.0.1:$WEBSOCKIFY_PORT"
    return 0
  fi
  setsid websockify "--web=$NOVNC_WEB" \
    "127.0.0.1:$WEBSOCKIFY_PORT" "127.0.0.1:$VNC_PORT" \
    >>"$DESKTOP_LOG_DIR/websockify.log" 2>&1 &
  CHILD_PID[websockify]=$!
  echo "$CHILD_PID[websockify]" > "$RUN_DIR/supervisor-websockify.pid"
  log "websockify started (pid ${CHILD_PID[websockify]})"
}

start_tunnel() {
  # A quick tunnel needs no credential, so it always starts when cloudflared is
  # installed.  It is still optional: without cloudflared the app is reachable
  # through the Codespaces port forward and the screen works over /ws/screen.
  if ! command -v "$CLOUDFLARED_BIN" >/dev/null 2>&1; then
    log "cloudflared not installed - public tunnel NOT started (Codespaces port forward only)"
    return 0
  fi
  setsid bash "$CODESPACE_DIR/start-tunnel.sh" \
    >>"$DESKTOP_LOG_DIR/cloudflared.log" 2>&1 &
  CHILD_PID[tunnel]=$!
  echo "$CHILD_PID[tunnel]" > "$RUN_DIR/supervisor-tunnel.pid"
  log "cloudflared started (pid ${CHILD_PID[tunnel]})"
}

child_alive() {
  local pid="${CHILD_PID[$1]:-}"
  [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}

ensure_websockify() {
  listening "$WEBSOCKIFY_PORT" && return 0
  if child_alive websockify; then
    # Process is up but not accepting yet; give it a moment before assuming
    # it is wedged.
    for _ in $(seq 1 10); do
      listening "$WEBSOCKIFY_PORT" && return 0
      sleep 1
    done
    log "websockify is alive but not accepting on $WEBSOCKIFY_PORT; restarting"
    kill -9 "${CHILD_PID[websockify]}" 2>/dev/null
    sleep 1
  fi
  start_websockify
}

# --- first pass ------------------------------------------------------------
start_agent
start_websockify
start_tunnel

# --- health loop -----------------------------------------------------------
TICK=0
while true; do
  sleep 5
  TICK=$((TICK + 1))

  if ! child_alive agent; then
    log "agent died; restarting"
    start_agent
  fi

  ensure_websockify

  if ! child_alive tunnel; then
    log "cloudflared died; restarting"
    start_tunnel
  fi

  # A periodic one-line status makes the Codespace log useful on its own.  The
  # tunnel hostname goes in the log rather than the UI, because it changes on
  # every restart and an operator comparing it against /health is the whole
  # point of the line.
  if [ $((TICK % 12)) -eq 0 ]; then
    x_state="down"
    [ -S "/tmp/.X11-unix/X${DESKTOP_DISPLAY#:}" ] && x_state="up"
    vnc_state="down"; listening "$VNC_PORT" && vnc_state="up"
    ws_state="down";  listening "$WEBSOCKIFY_PORT" && ws_state="up"
    chrome_state="down"; listening "$CHROME_DEBUG_PORT" && chrome_state="up"
    tunnel_state="none"
    if [ -s "$PUBLIC_URL_FILE" ]; then tunnel_state="live"; fi
    log "status x=$x_state chrome=$chrome_state rfb=$vnc_state ws=$ws_state tunnel=$tunnel_state"
    if [ "$tunnel_state" = "live" ]; then
      log "  $(cat "$PUBLIC_URL_FILE")"
    fi
  fi
done
