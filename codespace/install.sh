#!/usr/bin/env bash
# One-time setup for the Codespace: installs the browser, the VNC chain and
# cloudflared.  Safe to re-run; every step is idempotent.
#
#   sudo bash codespace/install.sh
set -euo pipefail

CODESPACE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$CODESPACE_DIR/env.sh"

SUDO=""
[ "$(id -u)" -ne 0 ] && SUDO="sudo"

log "updating apt index"
$SUDO apt-get update -qq

log "installing the X/VNC/noVNC stack"
DEBIAN_FRONTEND=noninteractive $SUDO apt-get install -y --no-install-recommends \
  xvfb \
  fluxbox \
  x11vnc \
  x11-utils \
  novnc \
  websockify \
  ca-certificates \
  curl \
  gnupg \
  fonts-liberation \
  libnss3 \
  libatk-bridge2.0-0 \
  libgtk-3-0 \
  libgbm1 \
  libasound2t64 \
  procps \
  net-tools \
  iproute2

# --- Google Chrome (NOT Chromium) -----------------------------------------
if [ -x /usr/bin/google-chrome ]; then
  log "google-chrome already installed: $(/usr/bin/google-chrome --version)"
else
  log "installing Google Chrome from Google's apt repository"
  install -d -m 0755 /usr/share/keyrings
  curl -fsSL https://dl.google.com/linux/linux_signing_key.pub \
    | gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg
  chmod 0644 /usr/share/keyrings/google-chrome.gpg
  echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] https://dl.google.com/linux/chrome/deb/ stable main" \
    > /etc/apt/sources.list.d/google-chrome.list
  $SUDO apt-get update -qq
  DEBIAN_FRONTEND=noninteractive $SUDO apt-get install -y --no-install-recommends google-chrome-stable
fi

# --- cloudflared -----------------------------------------------------------
if command -v cloudflared >/dev/null 2>&1; then
  log "cloudflared already installed: $(cloudflared --version)"
else
  log "installing cloudflared"
  ARCH="$(dpkg --print-architecture)"
  case "$ARCH" in
    amd64) CF_ARCH=amd64 ;;
    arm64) CF_ARCH=arm64 ;;
    *) log "FATAL: unsupported architecture: $ARCH"; exit 1 ;;
  esac
  curl -fsSL -o /tmp/cloudflared.deb \
    "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${CF_ARCH}.deb"
  $SUDO apt-get install -y /tmp/cloudflared.deb
  rm -f /tmp/cloudflared.deb
fi

# --- Python deps for the agent --------------------------------------------
log "installing backend + agent Python dependencies"
$SUDO apt-get install -y --no-install-recommends python3-pip python3-venv
# The old form of this line was `... 2>/dev/null || log "WARN"`, which threw away
# pip's error *and* its exit status.  A failed install of uvicorn therefore
# presented as nothing at all until boot.sh reported "the app is not up", with
# the real reason sitting in a log nobody was reading.  Requirements and extras
# are installed separately so one optional failure cannot take uvicorn with it,
# and a failure is now loud.
if $SUDO pip3 install --quiet --break-system-packages -r "$REPO_ROOT/backend/requirements.txt"; then
  log "  ok  backend requirements"
else
  log "FATAL: backend requirements failed to install; the app will not start."
  log "       run this without --quiet to see which package broke."
  exit 1
fi

# Optional extras: nice to have, genuinely not required to serve the screen.
if $SUDO pip3 install --quiet --break-system-packages websocket-client selenium pillow; then
  log "  ok  agent extras (websocket-client, selenium, pillow)"
else
  log "WARN: some agent extras failed; the agent falls back without them"
fi

# Prove the app's own imports work, so boot.sh does not discover the problem.
if python3 -c "import uvicorn, fastapi, websockets" 2>/dev/null; then
  log "  ok  uvicorn + fastapi + websockets import cleanly"
else
  log "FATAL: uvicorn/fastapi/websockets are not importable by python3"
  exit 1
fi

# --- persistent profile ----------------------------------------------------
mkdir -p "$CHROME_PROFILE" "$DESKTOP_RUN_DIR" "$WORKSPACE"
log "chrome profile: $CHROME_PROFILE"
log "workspace:      $WORKSPACE"

log "verifying"
for cmd in Xvfb fluxbox x11vnc websockify google-chrome cloudflared; do
  command -v "$cmd" >/dev/null 2>&1 && log "  ok  $cmd" || log "  MISSING  $cmd"
done
[ -d "$NOVNC_WEB" ] && log "  ok  novnc web root ($NOVNC_WEB)" || log "  MISSING  novnc web root ($NOVNC_WEB)"

log "done. Next:"
log "  1. set RAG_AUTH_TOKEN (a Codespaces secret, or backend/.env) -- the app"
log "     refuses every route without it, on purpose"
log "  2. run codespace/boot.sh"
