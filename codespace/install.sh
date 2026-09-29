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
  iproute2 \
  ffmpeg \
  xdotool

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

# --- frontend dependencies ---------------------------------------------------
# This step was simply missing, so Frontend/node_modules only ever existed if
# somebody ran npm by hand.  boot.sh then tried `npm run build` on a machine
# with no vite and no tsc, the build failed, and -- because the app falls back
# to the bundled prototype when Frontend/dist is absent -- the deployment came
# up serving a stale page with no login screen and no screen view.
if ! command -v npm >/dev/null 2>&1; then
  log "installing node + npm (needed to build the UI)"
  $SUDO apt-get install -y --no-install-recommends nodejs npm
fi

if [ -f "$REPO_ROOT/Frontend/package-lock.json" ]; then
  log "installing frontend dependencies (npm ci)"
  if (cd "$REPO_ROOT/Frontend" && npm ci --no-audit --no-fund); then
    log "  ok  frontend dependencies installed from the lockfile"
  else
    log "FATAL: npm ci failed; the UI cannot be built without these"
    exit 1
  fi
else
  log "FATAL: Frontend/package-lock.json is missing, so dependencies cannot be pinned"
  log "       regenerate it with: (cd Frontend && npm install)"
  exit 1
fi

# Build here rather than leaving it to boot.sh, so a broken build is reported by
# the thing that installed the dependencies instead of surfacing later as a
# fallback prototype.
if (cd "$REPO_ROOT/Frontend" && npm run build); then
  log "  ok  frontend built (Frontend/dist)"
else
  log "FATAL: the frontend build failed; the app would serve the fallback prototype"
  exit 1
fi

# --- persistent profile ----------------------------------------------------
mkdir -p "$CHROME_PROFILE" "$DESKTOP_RUN_DIR" "$WORKSPACE"
log "chrome profile: $CHROME_PROFILE"
log "workspace:      $WORKSPACE"

log "verifying"
# ffmpeg and xdotool are in this list for a reason: ffmpeg is what the desktop
# stream and the AI's screenshots are captured with, and xdotool is what moves
# the real pointer when the AI clicks.  Both were previously assumed and never
# installed, so both features failed on a fresh Codespace.
for cmd in Xvfb fluxbox x11vnc websockify google-chrome cloudflared ffmpeg xdotool; do
  command -v "$cmd" >/dev/null 2>&1 && log "  ok  $cmd" || log "  MISSING  $cmd"
done
[ -d "$NOVNC_WEB" ] && log "  ok  novnc web root ($NOVNC_WEB)" || log "  MISSING  novnc web root ($NOVNC_WEB)"

log "done. Next:"
log "  1. set your provider keys (Codespaces secrets, or backend/.env)"
log "  2. run codespace/boot.sh"
