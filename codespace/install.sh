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
  net-tools

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
$SUDO pip3 install --quiet --break-system-packages \
  -r "$REPO_ROOT/backend/requirements.txt" \
  websocket-client \
  selenium \
  pillow 2>/dev/null || log "WARN: some python packages failed; the agent may fall back"

# --- persistent profile ----------------------------------------------------
mkdir -p "$CHROME_PROFILE" "$DESKTOP_RUN_DIR" "$WORKSPACE"
log "chrome profile: $CHROME_PROFILE"
log "workspace:      $WORKSPACE"

log "verifying"
for cmd in Xvfb fluxbox x11vnc websockify google-chrome cloudflared; do
  command -v "$cmd" >/dev/null 2>&1 && log "  ok  $cmd" || log "  MISSING  $cmd"
done
[ -d "$NOVNC_WEB" ] && log "  ok  novnc web root ($NOVNC_WEB)" || log "  MISSING  novnc web root ($NOVNC_WEB)"

log "done. Next: add CF_TUNNEL_TOKEN as a Codespace secret, then run codespace/supervise.sh"
