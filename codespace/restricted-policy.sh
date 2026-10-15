#!/usr/bin/env bash
# The owner's tool for the restricted-browsing allowlist.
#
# This is deliberately NOT something the agent can call.  It edits
# /etc/rag-restricted/allowlist, which is root-owned, and it refuses to run
# unless it is root.  The ordinary agent runs unprivileged and inside the
# sandbox (see restricted-sandbox.sh), so it neither owns the file nor has a
# route to the internet that ignores it.  A change made here takes effect on the
# next request: the policy re-reads the file when its mtime changes, so there is
# no restart and no window where a removed host is still reachable.
#
# Usage:
#   sudo codespace/restricted-policy.sh list
#   sudo codespace/restricted-policy.sh add example.com
#   sudo codespace/restricted-policy.sh add example.com:443
#   sudo codespace/restricted-policy.sh remove example.com
#   sudo codespace/restricted-policy.sh test https://example.com/page
#
# The file format is documented in backend/restricted_allowlist.txt.
set -euo pipefail

RESTRICTED_DIR="${RESTRICTED_DIR:-/etc/rag-restricted}"
ALLOWLIST="${RESTRICTED_ALLOWLIST_PATH:-$RESTRICTED_DIR/allowlist}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
BACKEND_DIR="$REPO_ROOT/backend"

log() { printf '[restricted-policy] %s\n' "$*" >&2; }

# The Codespace has python3; a workstation may only have python.  Pick whichever
# exists so the validator runs the shipping parser either way.
PY="${PYTHON:-$(command -v python3 || command -v python || true)}"
if [ -z "$PY" ]; then
  log "FATAL: no python interpreter found (need python3)"
  exit 1
fi

# Root only.  Not a courtesy check: without it any process on the box, including
# one the agent started, could rewrite the boundary.
if [ "$(id -u)" != 0 ]; then
  log "refused: this is the owner's allowlist tool and must run as root."
  log "try: sudo $0 $*"
  exit 1
fi

# The validator is the shipping parser, so a rule this tool accepts is a rule the
# gateway enforces.  A second, hand-rolled validator here would be a way to
# approve something the runtime later refuses -- or worse, the reverse.
validate() {
  local rule="$1"
  ( cd "$BACKEND_DIR" && PYTHONPATH="$BACKEND_DIR" "$PY" -c \
      'import sys; from app.restricted.policy import parse_rule; sys.exit(0 if parse_rule(sys.argv[1]) else 1)' \
      "$rule" ) 2>/dev/null
}

ensure_dir() {
  if [ ! -d "$RESTRICTED_DIR" ]; then
    mkdir -p "$RESTRICTED_DIR"
    log "created $RESTRICTED_DIR"
  fi
  chown root:root "$RESTRICTED_DIR" 2>/dev/null || true
  chmod 755 "$RESTRICTED_DIR" 2>/dev/null || true
  if [ ! -e "$ALLOWLIST" ]; then
    # Default deny: an empty file allows nothing.
    printf '# Restricted-browsing allowlist.  Managed by restricted-policy.sh.\n' > "$ALLOWLIST"
    log "created empty allowlist (deny by default): $ALLOWLIST"
  fi
  chown root:root "$ALLOWLIST" 2>/dev/null || true
  chmod 644 "$ALLOWLIST" 2>/dev/null || true
}

cmd_list() {
  ensure_dir
  log "allowlist: $ALLOWLIST"
  grep -vE '^\s*(#|$)' "$ALLOWLIST" || log "(empty -- nothing is allowed)"
}

cmd_add() {
  local rule="${1:-}"
  [ -n "$rule" ] || { log "usage: add <host[:port]> or <*.host>"; exit 2; }
  if ! validate "$rule"; then
    log "refused: '$rule' is not a valid rule (see backend/restricted_allowlist.txt)"
    exit 2
  fi
  ensure_dir
  if grep -qxF "$rule" "$ALLOWLIST"; then
    log "'$rule' is already approved"
    return 0
  fi
  printf '%s\n' "$rule" >> "$ALLOWLIST"
  log "approved '$rule'"
}

cmd_remove() {
  local rule="${1:-}"
  [ -n "$rule" ] || { log "usage: remove <host[:port]> or <*.host>"; exit 2; }
  ensure_dir
  if ! grep -qxF "$rule" "$ALLOWLIST"; then
    log "'$rule' was not in the allowlist"
    return 0
  fi
  # Exact match only, so removing one rule cannot silently drop a neighbour.
  grep -vxF "$rule" "$ALLOWLIST" > "$ALLOWLIST.tmp"
  mv "$ALLOWLIST.tmp" "$ALLOWLIST"
  chown root:root "$ALLOWLIST"
  chmod 644 "$ALLOWLIST"
  log "removed '$rule'"
}

cmd_test() {
  local url="${1:-}"
  [ -n "$url" ] || { log "usage: test <url>"; exit 2; }
  ensure_dir
  ( cd "$BACKEND_DIR" && PYTHONPATH="$BACKEND_DIR" RESTRICTED_ALLOWLIST_PATH="$ALLOWLIST" "$PY" -c '
import sys
from types import SimpleNamespace
from app.restricted.guard import Guard
from app.restricted.policy import load_policy
settings = SimpleNamespace(restricted_allowlist_path=sys.argv[1])
policy = load_policy(settings)
url = sys.argv[2]
verdict = Guard().check(url)
if not verdict.allowed:
    print(f"DENY  {url}\n      {verdict.reason}")
    sys.exit(3)
decision = policy.allows(verdict.host, verdict.port)
print(("ALLOW " if decision.allowed else "DENY  ") + url)
if not decision.allowed:
    print("      " + decision.reason)
    sys.exit(3)
' "$ALLOWLIST" "$url" )
}

case "${1:-}" in
  list|show) cmd_list ;;
  add) shift; cmd_add "${1:-}" ;;
  remove|rm) shift; cmd_remove "${1:-}" ;;
  test) shift; cmd_test "${1:-}" ;;
  *)
    cat >&2 <<EOF
usage: $0 <command> [argument]

  list                 show the approved destinations
  add <rule>           approve a host, host:port or *.host
  remove <rule>        withdraw a rule (exact match)
  test <url>           report whether a URL is allowed, and why not if it is not

The allowlist lives at $ALLOWLIST and is edited only as root.
EOF
    exit 2
    ;;
esac
