#!/usr/bin/env bash
# Deploy and verify the network boundary for restricted browsing.
#
# The allowlist and the gateway are only a boundary if the browser and the
# ordinary-agent shell cannot reach the internet any other way.  This script puts
# them in a Docker network with no external route and the gateway in the only
# network that has one, then *tests* that claim rather than asserting it: after
# starting, it execs into the browser and shell containers and tries to open a
# raw socket to a public address.  The connection must fail.  If it succeeds, the
# boundary is not real, the script tears the stack down and says so.
#
# Usage:
#   codespace/restricted-sandbox.sh up        # build if needed, start, verify
#   codespace/restricted-sandbox.sh verify    # re-run the boundary checks
#   codespace/restricted-sandbox.sh status
#   codespace/restricted-sandbox.sh logs
#   codespace/restricted-sandbox.sh down      # stop and remove
#
# Requires Docker with the compose plugin.  Codespaces images often include it,
# but a plain unprivileged container cannot create networks, so this may have to
# run where the Docker daemon is available.  When Docker is absent the script
# refuses and prints exactly what is missing -- it never pretends the allowlist
# is enforced when it is not.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
COMPOSE_FILE="$SCRIPT_DIR/restricted-compose.yml"
IMAGE="${RESTRICTED_IMAGE:-rag-restricted:latest}"
ALLOWLIST="${RESTRICTED_ALLOWLIST_PATH:-/etc/rag-restricted/allowlist}"
WORKSPACE="${WORKSPACE:-$REPO_ROOT}"

# A public address that is not the gateway.  Used to prove egress is closed.
PROBE_ADDR="${RESTRICTED_PROBE_ADDR:-1.1.1.1}"
PROBE_PORT="${RESTRICTED_PROBE_PORT:-443}"

log() { printf '[restricted-sandbox] %s\n' "$*" >&2; }

require_docker() {
  if ! command -v docker >/dev/null 2>&1; then
    log "Docker is not installed.  The network boundary cannot be created without it."
    log "Install Docker (or run on a host whose daemon can create networks) and retry."
    return 1
  fi
  if ! docker info >/dev/null 2>&1; then
    log "The Docker daemon is not reachable.  On a default Codespace the outer"
    log "container is unprivileged, so a nested Docker daemon must be started"
    log "(docker-in-docker) or this must run where a daemon is available."
    return 1
  fi
  if ! docker compose version >/dev/null 2>&1; then
    log "The Docker compose plugin is missing; install docker-compose-plugin."
    return 1
  fi
}

dc() {
  RESTRICTED_ALLOWLIST_PATH="$ALLOWLIST" WORKSPACE="$WORKSPACE" \
    docker compose -f "$COMPOSE_FILE" "$@"
}

build_image() {
  if docker image inspect "$IMAGE" >/dev/null 2>&1; then
    log "image $IMAGE already present"
    return 0
  fi
  log "building $IMAGE (this installs Chrome once; it is slow)"
  docker build -f "$SCRIPT_DIR/restricted.Dockerfile" -t "$IMAGE" "$REPO_ROOT"
}

# The verification is the reason to trust this at all.
verify() {
  local ok=0
  local py='import socket, sys
host, port, want = sys.argv[1], int(sys.argv[2]), sys.argv[3]
try:
    socket.create_connection((host, port), timeout=4).close()
    reachable = True
except OSError:
    reachable = False
print("reachable" if reachable else "blocked")
sys.exit(0 if (reachable == (want == "reachable")) else 1)'

  for svc in browser shell; do
    # 1. Egress to a public address must be blocked.
    if dc exec -T "$svc" python3 -c "$py" "$PROBE_ADDR" "$PROBE_PORT" blocked >/dev/null 2>&1; then
      log "PASS  $svc cannot reach $PROBE_ADDR:$PROBE_PORT"
    else
      log "FAIL  $svc reached $PROBE_ADDR:$PROBE_PORT -- egress is NOT closed"
      ok=1
    fi
    # 2. The gateway must be reachable, or nothing works anyway.
    if dc exec -T "$svc" python3 -c "$py" gateway 9111 reachable >/dev/null 2>&1; then
      log "PASS  $svc can reach the gateway"
    else
      log "FAIL  $svc cannot reach the gateway"
      ok=1
    fi
  done

  if [ "$ok" != 0 ]; then
    log "boundary verification FAILED; the allowlist is not enforced.  Bringing the stack down."
    dc down >/dev/null 2>&1 || true
    return 1
  fi
  log "boundary verified: the gateway is the only egress."
}

cmd_up() {
  require_docker
  if [ ! -e "$ALLOWLIST" ]; then
    log "allowlist $ALLOWLIST does not exist."
    log "create it first, e.g.:  sudo codespace/restricted-policy.sh list"
    return 1
  fi
  build_image
  log "starting the restricted stack"
  dc up -d
  verify
  log "up.  Point the backend at it:"
  log "  RESTRICTED_ENABLED=true"
  log "  RESTRICTED_BROWSER_URL=http://127.0.0.1:9110"
  log "  RESTRICTED_GATEWAY_URL=http://127.0.0.1:9111"
  log "  RESTRICTED_SHELL_URL=http://127.0.0.1:9112"
}

cmd_down() {
  require_docker
  dc down
}

cmd_status() {
  require_docker
  dc ps
}

cmd_logs() {
  require_docker
  dc logs --tail=80
}

case "${1:-up}" in
  up) cmd_up ;;
  down) cmd_down ;;
  status|ps) cmd_status ;;
  logs) cmd_logs ;;
  verify) require_docker; verify ;;
  *)
    log "usage: $0 [up|down|status|logs|verify]"
    exit 2
    ;;
esac
