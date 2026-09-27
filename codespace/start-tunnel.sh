#!/usr/bin/env bash
# Publish the RAG Agents app with a Cloudflare Quick Tunnel.
#
#   browser -> https://<random>.trycloudflare.com
#           -> cloudflared (this process, outbound only)
#           -> 127.0.0.1:8000  the app
#                 /websockify -> 127.0.0.1:6080 websockify -> RFB 5900 -> Chrome
#
# Only :8000 is published.  5900 and 6080 keep binding 127.0.0.1, so the framebuffer
# has no public port and the only route in runs through the app's session check.
#
# A quick tunnel needs no token, no domain and no config file: cloudflared picks a
# random trycloudflare.com hostname and prints it.  The catch is that the hostname
# changes on every restart, so this script records it where the backend can find
# it (PUBLIC_URL_FILE) and the frontend re-reads it via /screen/config.  That is
# the whole mechanism by which "the tunnel moved" becomes a non-event.
#
# The URL is deliberately not treated as a secret.  It is public by design; the
# passphrase (RAG_AUTH_TOKEN) is what decides who gets in.
set -uo pipefail

CODESPACE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$CODESPACE_DIR/env.sh"

# https://<name>.trycloudflare.com, the shape cloudflared prints.  Kept as a
# function-level constant so the script can be sourced by a test without
# running anything.
URL_RE='https://[a-z0-9][a-z0-9-]*\.trycloudflare\.com'

# Pull every tunnel URL out of a stream of cloudflared's log lines and record
# the most recent one.
#
# sed -n keeps scanning after a match rather than stopping at the first, so a
# reconnect that prints a fresh hostname overwrites the file instead of leaving
# the old one advertised.
scan() {
  # The delimiter is | and not / because the pattern contains "https://" --
  # with a / delimiter sed would end the pattern at the second slash and fail
  # with "unknown option to s", which silently means the URL is never found.
  sed -un -e "s|.*\($URL_RE\).*|\1|p" | while read -r url; do
    [ -n "$url" ] || continue
    # Write to a temp file and rename: /screen/config may read at any moment and
    # must never see a half-written URL.
    printf '%s' "$url" > "$PUBLIC_URL_FILE.tmp"
    mv -f "$PUBLIC_URL_FILE.tmp" "$PUBLIC_URL_FILE"
    log "public URL: $url"
  done
}

main() {
  require_cmd "$CLOUDFLARED_BIN" || exit 1

  # Wait for the app, because a tunnel that 502s every request is worse than no
  # tunnel: the user gets a hostname that appears broken.
  if ! listening "$BACKEND_PORT"; then
    log "waiting for the app on 127.0.0.1:$BACKEND_PORT (up to 120s)"
    for _ in $(seq 1 120); do
      listening "$BACKEND_PORT" && break
      sleep 1
    done
    if ! listening "$BACKEND_PORT"; then
      log "WARNING: nothing on 127.0.0.1:$BACKEND_PORT; the tunnel will serve 502s."
    fi
  fi

  # Anything left from a previous run describes a hostname that no longer
  # exists, and a stale entry is worse than none: the app would advertise a URL
  # Cloudflare has already forgotten.
  rm -f "$PUBLIC_URL_FILE"
  mkdir -p "$(dirname "$PUBLIC_URL_FILE")"

  log "starting quick tunnel -> $TUNNEL_ORIGIN"
  log "  the app answers here; /websockify is proxied to 127.0.0.1:$WEBSOCKIFY_PORT"

  "$CLOUDFLARED_BIN" tunnel --no-autoupdate --url "$TUNNEL_ORIGIN" 2>&1 | scan
  # PIPESTATUS is the only way to tell cloudflared's own exit code from the
  # status of the filter on the other end of the pipe.
  local status=${PIPESTATUS[0]}

  if [ "$status" -ne 0 ] && [ "$status" -ne 143 ]; then
    log "cloudflared exited with $status"
  fi
  # A dead tunnel must not leave behind a URL that will not answer.
  rm -f "$PUBLIC_URL_FILE"
  exit "$status"
}

# Sourcing this file gives a test the scan logic without starting a tunnel.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  main "$@"
fi
