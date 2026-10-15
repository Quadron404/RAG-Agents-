#!/usr/bin/env bash
# Run the restricted services on the host loopback -- DEVELOPMENT ONLY.
#
# !!! THIS IS NOT A SECURITY BOUNDARY !!!
#
# With no firewall between these processes and the internet, Chrome can ignore
# its --proxy-server setting and the shell can reach anything, so the allowlist is
# advisory here.  It exists to exercise the code and the tests, and to shape a
# local run.  For the boundary that actually holds, use
# codespace/restricted-sandbox.sh, which puts the browser and the shell where the
# gateway is their only egress.
#
# Usage:
#   codespace/restricted-dev.sh up     # start gateway, browser, shell on loopback
#   codespace/restricted-dev.sh down
#   codespace/restricted-dev.sh status
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
BACKEND_DIR="$REPO_ROOT/backend"
RUN_DIR="${RESTRICTED_RUN_DIR:-/tmp/rag-restricted}"
ALLOWLIST="${RESTRICTED_ALLOWLIST_PATH:-$RUN_DIR/allowlist}"
WORKSPACE="${WORKSPACE:-$REPO_ROOT}"

log() { printf '[restricted-dev] %s\n' "$*" >&2; }

PY="${PYTHON:-$(command -v python3 || command -v python || true)}"

warn() {
  log "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
  log "!! DEVELOPMENT MODE -- the allowlist is NOT network-enforced.  !!"
  log "!! Any process here can still reach the internet directly.     !!"
  log "!! Use restricted-sandbox.sh for a real boundary.              !!"
  log "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
}

port_up() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null && exec 3<&- && return 0; return 1; }

ensure_allowlist() {
  mkdir -p "$RUN_DIR"
  if [ ! -e "$ALLOWLIST" ]; then
    cp "$BACKEND_DIR/restricted_allowlist.txt" "$ALLOWLIST"
    log "seeded $ALLOWLIST from backend/restricted_allowlist.txt"
  fi
}

start() {
  local name="$1"; shift
  local pidfile="$RUN_DIR/$name.pid"
  if [ -f "$pidfile" ] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
    log "$name already running (pid $(cat "$pidfile"))"
    return 0
  fi
  ( cd "$BACKEND_DIR" && PYTHONPATH="$BACKEND_DIR" nohup "$PY" "$@" >"$RUN_DIR/$name.log" 2>&1 & echo $! > "$pidfile" )
  log "$name started (pid $(cat "$pidfile")), log $RUN_DIR/$name.log"
}

cmd_up() {
  warn
  ensure_allowlist
  start gateway -m app.restricted.gateway --host 127.0.0.1 --port 9111 --allowlist "$ALLOWLIST"
  start browser -m app.restricted.browser_service --host 127.0.0.1 --port 9110 \
      --allowlist "$ALLOWLIST" --gateway http://127.0.0.1:9111 --profile "$RUN_DIR/profile"
  start shell   -m app.restricted.shell_service --host 127.0.0.1 --port 9112 --workdir "$WORKSPACE"
  log "set: RESTRICTED_ENABLED=true RESTRICTED_BROWSER_URL=http://127.0.0.1:9110"
  log "     RESTRICTED_GATEWAY_URL=http://127.0.0.1:9111 RESTRICTED_SHELL_URL=http://127.0.0.1:9112"
  log "     RESTRICTED_ALLOWLIST_PATH=$ALLOWLIST"
}

cmd_down() {
  for name in gateway browser shell; do
    local pidfile="$RUN_DIR/$name.pid"
    if [ -f "$pidfile" ]; then
      kill "$(cat "$pidfile")" 2>/dev/null || true
      rm -f "$pidfile"
      log "$name stopped"
    fi
  done
}

cmd_status() {
  for port in 9110 9111 9112; do
    if port_up "$port"; then log "port $port up"; else log "port $port down"; fi
  done
}

case "${1:-up}" in
  up) cmd_up ;;
  down) cmd_down ;;
  status) cmd_status ;;
  *) log "usage: $0 [up|down|status]"; exit 2 ;;
esac
