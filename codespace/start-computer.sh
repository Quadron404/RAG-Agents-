#!/usr/bin/env bash
# Bring up the real graphical session and the WebSocket bridge for it.
#
#   Xvfb :99  ->  Google Chrome (kiosk)  ->  x11vnc 127.0.0.1:5900
#             ->  websockify 127.0.0.1:6080  ->  noVNC over WebSocket
#
# Nothing here is recorded, screenshotted or re-encoded.  The pixels are the
# live framebuffer and the user's input goes back into the same X display that
# Chrome is drawing on.
#
# Normally codespace/supervise.sh runs the agent daemon, which supervises all
# of this already.  This script is the standalone path: it is what you run to
# recover a screen by hand, and what you use if you want the screen without
# the agent.  It only starts what is not already listening, so running it
# against a live stack is a no-op.
set -uo pipefail

CODESPACE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$CODESPACE_DIR/env.sh"

mkdir -p "$DESKTOP_RUN_DIR" "$CHROME_PROFILE" "$WORKSPACE"

pidfile() { echo "$DESKTOP_RUN_DIR/$1.pid"; }

alive() {
  local pf; pf="$(pidfile "$1")"
  [ -f "$pf" ] || return 1
  local pid; pid="$(cat "$pf" 2>/dev/null || true)"
  [ -n "$pid" ] || return 1
  kill -0 "$pid" 2>/dev/null
}

# --- 1) Xvfb ---------------------------------------------------------------
if [ -S "/tmp/.X11-unix/X${DESKTOP_DISPLAY#:}" ] && pgrep -x Xvfb >/dev/null 2>&1; then
  log "Xvfb already up on $DESKTOP_DISPLAY"
else
  log "starting Xvfb on $DESKTOP_DISPLAY ($DESKTOP_SIZE x $DESKTOP_DEPTH)"
  # -ac: x11vnc attaches as a local X client, so host-based access control is
  #      not wanted on a display that only exists inside this container.
  # -nolisten tcp: the X protocol itself is never reachable over the network.
  nohup Xvfb "$DESKTOP_DISPLAY" \
    -screen 0 "${DESKTOP_SIZE}x${DESKTOP_DEPTH}" \
    -ac -nolisten tcp -noreset \
    >"$DESKTOP_LOG_DIR/xvfb.log" 2>&1 &
  echo $! > "$(pidfile xvfb)"
  for _ in $(seq 1 60); do
    [ -S "/tmp/.X11-unix/X${DESKTOP_DISPLAY#:}" ] && break
    sleep 0.25
  done
  sleep 0.5
fi

if [ ! -S "/tmp/.X11-unix/X${DESKTOP_DISPLAY#:}" ]; then
  log "FATAL: X display $DESKTOP_DISPLAY never came up; see $DESKTOP_LOG_DIR/xvfb.log"
  exit 1
fi

# --- 2) window manager ------------------------------------------------------
# Kept underneath Chrome on purpose: kiosk hides it, but it is what makes the
# display behave like a real desktop if the user ever needs a menu or a dialog.
if pgrep -f "$DESKTOP_WM" >/dev/null 2>&1; then
  log "$DESKTOP_WM already running"
else
  log "starting $DESKTOP_WM"
  nohup "$DESKTOP_WM" >"$DESKTOP_LOG_DIR/fluxbox.log" 2>&1 &
  echo $! > "$(pidfile wm)"
  sleep 1
fi

# --- 3) Google Chrome ------------------------------------------------------
# --kiosk makes Chrome fill the whole display, so the user sees the browser
# rather than a desktop with a window on it.  The profile is persistent, so a
# manual site login survives every later start.
if listening "$CHROME_DEBUG_PORT"; then
  log "Chrome already serving CDP on $CHROME_DEBUG_PORT"
else
  log "starting Google Chrome (kiosk, profile $CHROME_PROFILE)"
  CHROME_BIN="${CHROME_BINARY:-$(command -v google-chrome || command -v google-chrome-stable || echo /usr/bin/google-chrome)}"
  nohup "$CHROME_BIN" \
    --no-sandbox \
    --disable-dev-shm-usage \
    --no-first-run \
    --no-default-browser-check \
    --remote-allow-origins=* \
    "--remote-debugging-port=$CHROME_DEBUG_PORT" \
    "--user-data-dir=$CHROME_PROFILE" \
    --kiosk \
    --start-maximized \
    "--window-size=${DESKTOP_SIZE%x*},${DESKTOP_SIZE#*x}" \
    --window-position=0,0 \
    "$CHROME_START_URL" \
    >"$DESKTOP_LOG_DIR/chromium.log" 2>&1 &
  echo $! > "$(pidfile chromium)"
fi

# --- 4) x11vnc -------------------------------------------------------------
if listening "$VNC_PORT"; then
  log "x11vnc already listening on 127.0.0.1:$VNC_PORT"
else
  log "starting x11vnc on 127.0.0.1:$VNC_PORT"
  # -localhost is the important one: RFB must never answer on 0.0.0.0.
  nohup x11vnc \
    -display "$DESKTOP_DISPLAY" \
    -rfbport "$VNC_PORT" \
    -localhost \
    -forever \
    -shared \
    -repeat \
    -noxdamage \
    -nolookup \
    -quiet \
    -nopw \
    >"$DESKTOP_LOG_DIR/x11vnc.log" 2>&1 &
  echo $! > "$(pidfile vnc)"
  for _ in $(seq 1 40); do
    listening "$VNC_PORT" && break
    sleep 0.25
  done
fi

listening "$VNC_PORT" || { log "FATAL: x11vnc never came up; see $DESKTOP_LOG_DIR/x11vnc.log"; exit 1; }

# --- 5) websockify ---------------------------------------------------------
# Binds 127.0.0.1:6080 and serves the noVNC client from the same port, so the
# tunnel has exactly one origin to forward to.  websockify accepts the
# WebSocket upgrade on any path, which is why /websockify works here.
if listening "$WEBSOCKIFY_PORT"; then
  log "websockify already listening on 127.0.0.1:$WEBSOCKIFY_PORT"
else
  log "starting websockify on 127.0.0.1:$WEBSOCKIFY_PORT -> 127.0.0.1:$VNC_PORT"
  nohup websockify \
    "--web=$NOVNC_WEB" \
    "127.0.0.1:$WEBSOCKIFY_PORT" \
    "127.0.0.1:$VNC_PORT" \
    >"$DESKTOP_LOG_DIR/websockify.log" 2>&1 &
  echo $! > "$(pidfile websockify)"
  for _ in $(seq 1 40); do
    listening "$WEBSOCKIFY_PORT" && break
    sleep 0.25
  done
fi

listening "$WEBSOCKIFY_PORT" || { log "FATAL: websockify never came up; see $DESKTOP_LOG_DIR/websockify.log"; exit 1; }

# --- report ----------------------------------------------------------------
log "stack is up:"
log "  display    $DESKTOP_DISPLAY ($DESKTOP_SIZE)"
log "  chrome     $(command -v google-chrome >/dev/null 2>&1 && google-chrome --version || echo 'not found')"
log "  RFB        127.0.0.1:$VNC_PORT (loopback only)"
log "  websocket  127.0.0.1:$WEBSOCKIFY_PORT (loopback only)"
log "  noVNC test http://127.0.0.1:$WEBSOCKIFY_PORT/vnc.html"
log "bind check:"
ss -ltnp 2>/dev/null | grep -E ":($VNC_PORT|$WEBSOCKIFY_PORT)\b" || true
