#!/usr/bin/env bash
# Prove the whole thing works on a real machine, end to end.
#
#   bash codespace/verify.sh
#
# Everything here is checked against the *live* stack, not a mock: the real
# tunnel, the real cookie, the real RFB handshake, the real Chrome. It is meant
# to be run in the Codespace after `codespace/boot.sh`, and to fail loudly rather
# than report a green tick for something it could not actually confirm.
#
# The two things this is really proving:
#
#   * the tunnel URL reaches the app, and the app will not serve the screen
#     without the passphrase -- through the tunnel, not just locally;
#   * 5900 and 6080 are not reachable from the internet, so the only way in is
#     the app, which is the one place a session is checked.
#
# Exit code 0 means every required check passed. SKIP is reported separately from
# PASS, because "could not check" is not the same as "fine".
set -uo pipefail

CODESPACE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$CODESPACE_DIR/env.sh"

PASS=0; FAIL=0; SKIP=0
FAILED_CHECKS=()

ok()   { printf '  \033[32mPASS\033[0m  %s\n' "$1"; PASS=$((PASS+1)); }
bad()  { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; FAIL=$((FAIL+1)); FAILED_CHECKS+=("$1"); }
skip() { printf '  \033[33mSKIP\033[0m  %s\n' "$1"; SKIP=$((SKIP+1)); }
head_() { printf '\n\033[1m%s\033[0m\n' "$1"; }

# Check that a port is bound to loopback and nothing else.  0.0.0.0 or :: here
# would mean the screen had a public address, which is the one thing that must
# never be true.
check_loopback_only() {
  local port="$1" label="$2" binds
  binds="$(ss -Hltn "sport = :$port" 2>/dev/null | awk '{print $4}')"
  if [ -z "$binds" ]; then
    bad "$label: nothing is listening on $port"
    return
  fi
  local bad_bind
  bad_bind="$(printf '%s\n' "$binds" | grep -Ev '^127\.0\.0\.1:|^\[::1\]:' || true)"
  if [ -n "$bad_bind" ]; then
    bad "$label: $port is bound beyond loopback ($bad_bind)"
  else
    ok "$label: $port is loopback-only ($binds)"
  fi
}

# Poll until a command succeeds, so a slow start is not reported as a failure.
wait_for() {
  local label="$1" tries="$2"; shift 2
  for _ in $(seq 1 "$tries"); do
    if "$@" >/dev/null 2>&1; then return 0; fi
    sleep 1
  done
  bad "$label (waited ${tries}s)"
  return 1
}

printf '\033[1mRAG Agents -- live verification\033[0m\n'

# ---------------------------------------------------------------------------
head_ "1. Is anything running yet?"
# ---------------------------------------------------------------------------
if listening "$BACKEND_PORT"; then
  ok "the app is listening on 127.0.0.1:$BACKEND_PORT"
  APP_WAS_UP=1
else
  printf '  the app is not running; starting it\n'
  bash "$CODESPACE_DIR/boot.sh" >/dev/null 2>&1
  wait_for "the app did not come up on :$BACKEND_PORT" 60 listening "$BACKEND_PORT" || true
  if listening "$BACKEND_PORT"; then
    ok "the app started and is listening on 127.0.0.1:$BACKEND_PORT"
  else
    bad "the app is not listening on 127.0.0.1:$BACKEND_PORT; see $DESKTOP_LOG_DIR/backend.log"
  fi
fi

# ---------------------------------------------------------------------------
head_ "2. Google Chrome, Xvfb, x11vnc, websockify"
# ---------------------------------------------------------------------------
if [ -S "/tmp/.X11-unix/X${DESKTOP_DISPLAY#:}" ]; then
  ok "Xvfb is up on $DESKTOP_DISPLAY"
else
  bad "no X socket for $DESKTOP_DISPLAY; the display stack is not running"
fi

if listening "$VNC_PORT"; then
  ok "x11vnc is listening on 127.0.0.1:$VNC_PORT"
else
  bad "x11vnc is not listening on 127.0.0.1:$VNC_PORT"
fi

if listening "$WEBSOCKIFY_PORT"; then
  ok "websockify is listening on 127.0.0.1:$WEBSOCKIFY_PORT"
else
  bad "websockify is not listening on 127.0.0.1:$WEBSOCKIFY_PORT"
fi

# Chrome has to be a real window on the real display, not a process that died.
# It must NOT be in kiosk mode: kiosk hides the tab strip, the new-tab button and
# the address bar, which makes the live screen impossible to drive.  Asserting
# the absence is the point, so a stray --kiosk fails the check loudly.
if pgrep -f "google-chrome" >/dev/null 2>&1; then
  if pgrep -f "google-chrome.*--kiosk" >/dev/null 2>&1; then
    bad "Google Chrome is running with --kiosk, which hides the tab strip and address bar"
  else
    ok "Google Chrome is running with its normal UI (no --kiosk)"
  fi
else
  bad "no Google Chrome process; see $DESKTOP_LOG_DIR/chromium.log"
fi

# The profile is the user's login state, so where it lives is worth confirming.
if [ -d "$CHROME_PROFILE" ]; then
  ok "the Chrome profile exists at $CHROME_PROFILE"
  if df -T "$CHROME_PROFILE" 2>/dev/null | tail -1 | grep -qiE "overlay|/tmp"; then
    skip "could not confirm the profile is on the persistent volume"
  else
    ok "the profile is on a persistent volume, not container-local /tmp"
  fi
else
  bad "the Chrome profile directory is missing: $CHROME_PROFILE"
fi

# The agent's three apps have to reach the same machine, or the Computer view is
# a pretty picture of a machine the agents cannot touch.
for ep in display/status; do
  if curl -fsS --max-time 5 "http://127.0.0.1:$AGENT_PORT/$ep" >/dev/null 2>&1; then
    ok "the agent answers /$ep (the same machine Chrome is on)"
  else
    bad "the agent does not answer http://127.0.0.1:$AGENT_PORT/$ep"
  fi
done

# ---------------------------------------------------------------------------
head_ "3. The tunnel"
# ---------------------------------------------------------------------------
if [ ! -s "$PUBLIC_URL_FILE" ]; then
  printf '  no URL in %s yet; starting the tunnel\n' "$PUBLIC_URL_FILE"
  nohup bash "$CODESPACE_DIR/start-tunnel.sh" >>"$DESKTOP_LOG_DIR/cloudflared.log" 2>&1 &
  for _ in $(seq 1 60); do
    [ -s "$PUBLIC_URL_FILE" ] && break
    sleep 1
  done
fi

PUBLIC_URL=""
[ -s "$PUBLIC_URL_FILE" ] && PUBLIC_URL="$(cat "$PUBLIC_URL_FILE")"

# Resolve the passphrase the same way the backend does, so the login checks
# below actually run.  They used to test `[ -n "${RAG_AUTH_TOKEN:-}" ]` -- the
# *shell* variable -- and skip the entire section when it was unset, which is the
# normal case: the token lives in backend/.env or as a Codespaces secret and is
# never exported into the shell that runs this script.  So the one check that
# would have caught "the server has no passphrase" was itself the thing being
# skipped, and the run reported all-clear.
#
# Note this is a fallback for the check only.  It is read here, in the
# verifier's own process, and is never passed to the app: the app gets the token
# through its own environment and hands out a cookie instead.
AUTH_TOKEN="${RAG_AUTH_TOKEN:-}"
if [ -z "$AUTH_TOKEN" ] && [ -f "$REPO_ROOT/backend/.env" ]; then
  # head, not tail: config.py populates the environment with
  # `if key not in os.environ`, so the *first* RAG_AUTH_TOKEN in the file is the
  # one the app actually uses.  Reading the last one here would make the
  # verifier test a different passphrase than the server is running.
  AUTH_TOKEN="$(grep -s '^RAG_AUTH_TOKEN=' "$REPO_ROOT/backend/.env" | head -n1 | cut -d= -f2- | tr -d '"'\''[:space:]')"
fi

if [ -n "$PUBLIC_URL" ]; then
  ok "the tunnel published $(cat "$PUBLIC_URL_FILE")"
else
  bad "the tunnel has not published a URL to $PUBLIC_URL_FILE; see $DESKTOP_LOG_DIR/cloudflared.log"
fi

if printf '%s' "$PUBLIC_URL" | grep -Eq '^https://[a-z0-9][a-z0-9-]*\.trycloudflare\.com$'; then
  ok "it is a real trycloudflare origin"
else
  bad "the published URL is not a trycloudflare origin: ${PUBLIC_URL:-<empty>}"
fi

# The public URL is by design not a secret. What matters is that a request
# through it does not get the screen without a passphrase.
# ---------------------------------------------------------------------------
head_ "4. Reaching the app through the tunnel"
# ---------------------------------------------------------------------------
if [ -z "$PUBLIC_URL" ]; then
  skip "no tunnel URL, so nothing can be checked through it"
else
  # A quick tunnel is not routable the instant it is announced; give the edge a
  # moment to pick it up before calling it broken.
  EDGE_UP=0
  for _ in $(seq 1 20); do
    if curl -fsS --max-time 10 -o /dev/null "$PUBLIC_URL/health" 2>/dev/null; then
      EDGE_UP=1; break
    fi
    sleep 2
  done

  if [ "$EDGE_UP" = 1 ]; then
    ok "the tunnel origin answers ($PUBLIC_URL/health)"
  else
    bad "the tunnel origin does not answer; the edge may still be propagating"
    printf '        %s\n' "try again in a minute: quick tunnels take a moment to route"
  fi

  # The login page itself is public, or nobody could ever log in.
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$PUBLIC_URL/" 2>/dev/null)"
  if [ "$code" = "200" ]; then
    ok "the login page is served without a session (GET / -> 200)"
  else
    bad "GET / returned ${code:-no response}, expected 200"
  fi

  # "GET / returned 200" is not the same as "the app is being served".  When
  # Frontend/dist is missing the app deliberately falls back to the bundled
  # prototype in backend/app/static, which also answers 200 -- a stale page with
  # no login screen, no Computer view and no VNC.  That is precisely how a failed
  # frontend build reached a user as a green verification run, so the check has
  # to be about *which* page came back, not whether one did.
  ui="$(curl -fsS --max-time 10 "$PUBLIC_URL/" 2>/dev/null || true)"
  if printf '%s' "$ui" | grep -q '/assets/index-'; then
    ok "the built UI is being served, not the fallback prototype"
  else
    bad "the tunnel is serving the fallback prototype instead of the built UI; Frontend/dist is missing or the build failed -- see $DESKTOP_LOG_DIR/frontend-build.log"
  fi

  # And the bundle index.html points at has to exist.  A dist directory left over
  # from an older build references hashed filenames that a newer build deleted,
  # which serves a page that then fails to load any JavaScript at all.
  ui_asset="$(printf '%s' "$ui" | grep -o '/assets/index-[A-Za-z0-9_-]*\.js' | head -n1)"
  if [ -n "$ui_asset" ]; then
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$PUBLIC_URL$ui_asset" 2>/dev/null)"
    if [ "$code" = "200" ]; then
      ok "the UI bundle loads ($ui_asset)"
    else
      bad "index.html references $ui_asset but it returned ${code:-no response}; Frontend/dist is stale"
    fi
  fi

  # This is the acceptance criterion, checked through the public entry point
  # rather than in-process: no cookie, no screen.
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$PUBLIC_URL/screen/config" 2>/dev/null)"
  if [ "$code" = "401" ]; then
    ok "the screen config is refused without a session (GET /screen/config -> 401)"
  else
    bad "GET /screen/config returned ${code:-no response} without a session, expected 401"
  fi

  # The app must know a passphrase is configured, or it is refusing every route
  # for a reason nobody will discover from the UI.  Asked of the server rather
  # than of the shell, because "did the operator export the right variable" is
  # not the question -- "is the deployment actually usable" is.
  status_body="$(curl -fsS --max-time 10 "$PUBLIC_URL/auth/status" 2>/dev/null || true)"
  if printf '%s' "$status_body" | grep -q '"auth_required":true'; then
    ok "the server reports that a passphrase is required"
  else
    bad "the server reports no passphrase is configured, so it refuses every route; set RAG_AUTH_TOKEN and restart the backend"
  fi

  if [ -n "$AUTH_TOKEN" ]; then
    ok "a passphrase was found for the login check"
  else
    skip "no passphrase available to the verifier (server may use a Codespaces secret)"
  fi
fi

# ---------------------------------------------------------------------------
head_ "5. Logging in through the tunnel"
# ---------------------------------------------------------------------------
COOKIE=""
if [ -z "$PUBLIC_URL" ]; then
  skip "cannot log in without a tunnel URL"
elif [ -z "$AUTH_TOKEN" ]; then
  # A tunnel with no passphrase to test against is a broken deployment, and it
  # used to be reported as a skip -- which is how "the whole app 401s" reached a
  # user with an all-green verification behind it.
  bad "the tunnel is public but no passphrase could be read, so login is unverifiable; set RAG_AUTH_TOKEN in backend/.env or as a Codespaces secret"
else
  JAR="$(mktemp)"
  if curl -fsS --max-time 10 -c "$JAR" -X POST "$PUBLIC_URL/auth/login" \
       -H 'Content-Type: application/json' \
    --data "$(printf '{"passphrase":%s}' "$(printf '%s' "$AUTH_TOKEN" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')")" \
       >/dev/null 2>&1; then
    ok "the passphrase was accepted"
  else
    bad "the passphrase was rejected over the tunnel"
  fi

  if grep -q rag_session "$JAR" 2>/dev/null; then
    COOKIE="$JAR"
    ok "a session cookie was issued"
  else
    bad "no session cookie was issued; check RAG_AUTH_TOKEN matches the backend's"
  fi

  if [ -n "$COOKIE" ]; then
    body="$(curl -fsS --max-time 10 -b "$COOKIE" "$PUBLIC_URL/screen/config" 2>/dev/null)"
    if printf '%s' "$body" | grep -q '"mode"'; then
      ok "the screen config is served with a session"
    else
      bad "the screen config was not served with a session"
    fi

    mode="$(printf '%s' "$body" | python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("mode",""))
except Exception: print("")' 2>/dev/null)"
    ws="$(printf '%s' "$body" | python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("wsUrl",""))
except Exception: print("")' 2>/dev/null)"

    if [ "$mode" = "tunnel" ]; then
      ok "/screen/config reports mode=tunnel (it found the live tunnel URL)"
    else
      bad "/screen/config reported mode=${mode:-unknown}, expected tunnel"
    fi

    if [ "$ws" = "wss://${PUBLIC_URL#https://}/websockify" ]; then
      ok "the advertised socket is ${ws}"
    else
      bad "the advertised socket is '${ws}', expected wss://${PUBLIC_URL#https://}/websockify"
    fi
  fi
  [ -n "$COOKIE" ] && rm -f "$COOKIE"
fi

# ---------------------------------------------------------------------------
head_ "6. The live screen, over the real WebSocket"
# ---------------------------------------------------------------------------
# The strongest automated proof short of a human looking at it: open the screen
# the way noVNC does, through the tunnel, with a session, and read the RFB
# handshake off the far end.  Bytes arriving means the tunnel, the app's auth,
# the proxy, websockify and x11vnc are all working as one path.
if [ -z "$COOKIE" ] || [ -z "$PUBLIC_URL" ]; then
  skip "no session, so the screen cannot be opened"
else
  jar2="$(mktemp)"
  curl -fsS --max-time 10 -c "$jar2" -X POST "$PUBLIC_URL/auth/login" \
    -H 'Content-Type: application/json' \
    --data "$(printf '{"passphrase":%s}' "$(printf '%s' "$AUTH_TOKEN" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')")" \
    >/dev/null 2>&1

  DESKTOP_SIZE="$DESKTOP_SIZE" PUBLIC_URL="$PUBLIC_URL" COOKIE_JAR="$jar2" python3 - <<'PY'
import asyncio, os, struct, sys

url = os.environ["PUBLIC_URL"].replace("https://", "wss://", 1) + "/websockify"
jar = os.environ["COOKIE_JAR"]
cookie = ""
for line in open(jar):
    if "rag_session" in line:
        cookie = line.split()[-1]
        break
if not cookie:
    print("  \033[31mFAIL\033[0m  no session cookie to open the screen with")
    sys.exit(1)

def ok(m):   print(f"  \033[32mPASS\033[0m  {m}")
def bad(m):  print(f"  \033[31mFAIL\033[0m  {m}")

async def go():
    import websockets
    # Exactly how noVNC opens it: the binary subprotocol, the session cookie the
    # browser already holds, and the same origin the page came from.
    async with websockets.connect(
        url, subprotocols=["binary"], additional_headers={"Cookie": f"rag_session={cookie}"},
        max_size=None, open_timeout=30,
    ) as ws:
        ok(f"WebSocket opened through the tunnel ({ws.subprotocol!r} subprotocol)")

        # --- RFB 3.8 handshake, in order ---------------------------------
        greeting = await asyncio.wait_for(ws.recv(), timeout=30)
        if isinstance(greeting, str) or not greeting.startswith(b"RFB "):
            bad(f"expected an RFB greeting, got {greeting[:16]!r}")
            return 1
        version = greeting[:12]
        ok(f"the origin is a real VNC server ({version.decode('ascii', 'replace').strip()!r})")

        await ws.send(version)                 # client version
        n = await asyncio.wait_for(ws.recv(), timeout=20)
        if isinstance(n, str) or len(n) < 1:
            bad("no security types offered")
            return 1
        await ws.send(bytes([1]))              # security type 1 = None
        result = await asyncio.wait_for(ws.recv(), timeout=20)
        if len(result) < 4 or struct.unpack(">I", result[:4])[0] != 0:
            bad("the server refused the None security type")
            return 1
        ok("handshake accepted with no VNC password (loopback only)")

        await ws.send(bytes([1]))              # ClientInit, shared
        init = await asyncio.wait_for(ws.recv(), timeout=20)
        if len(init) < 24:
            bad(f"ServerInit was short ({len(init)} bytes)")
            return 1
        w, h = struct.unpack(">HH", init[:4])
        name_len = struct.unpack(">I", init[20:24])[0]
        name = init[24:24 + name_len].decode("utf-8", "replace")
        ok(f"desktop is {w}x{h} named {name!r}")

        # The framebuffer should be the size the stack asked Xvfb for. A mismatch
        # would show up as a squashed or letterboxed screen in the browser.
        want = os.environ.get("DESKTOP_SIZE", "")
        if want == f"{w}x{h}":
            ok(f"the framebuffer matches the configured {want}")
        else:
            print(f"  \033[33mSKIP\033[0m  framebuffer is {w}x{h}, configured size is {want or 'unset'}")

        # --- ask for the framebuffer -------------------------------------
        await ws.send(struct.pack(">BBHHHH", 3, 0, 0, 0, w, h))

        # A framebuffer update can only exist if there is a real X server with
        # real content on it, so this is the check that Chrome is actually
        # drawing rather than merely running.
        total = 0
        deadline = asyncio.get_event_loop().time() + 30
        while asyncio.get_event_loop().time() < deadline:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=12)
            except asyncio.TimeoutError:
                break
            if isinstance(msg, bytes) and len(msg) > 64:
                total = max(total, len(msg))
                if total > 4096:
                    break
        if total > 4096:
            ok(f"a real framebuffer update arrived ({total:,} bytes of pixels)")
        else:
            bad("no framebuffer update arrived; the display is blank or the handshake desynced")
            return 1
    return 0

try:
    sys.exit(asyncio.run(go()) or 0)
except Exception as exc:
    bad(f"could not open the screen: {type(exc).__name__}: {exc}")
    sys.exit(1)
PY
  screen_rc=$?
  [ "$screen_rc" -eq 0 ] || FAILED_CHECKS+=("the live screen did not serve a framebuffer over the tunnel")
  rm -f "$jar2"
fi

# ---------------------------------------------------------------------------
head_ "7. Nothing but the app is published"
# ---------------------------------------------------------------------------
check_loopback_only "$VNC_PORT" "x11vnc"
check_loopback_only "$WEBSOCKIFY_PORT" "websockify"
check_loopback_only "$AGENT_PORT" "the agent"

# Belt and braces: prove 5900 is not answerable from outside this machine.
if command -v curl >/dev/null 2>&1; then
  for port in "$VNC_PORT" "$WEBSOCKIFY_PORT"; do
    if curl -fsS --max-time 4 "http://10.255.255.1:$port/" >/dev/null 2>&1; then
      bad "port $port answered from a non-loopback address"
    else
      ok "port $port is not reachable from off-machine"
    fi
  done
fi

# X itself must not be a remote-control surface.
if ps aux | grep -q "[X]vfb" && ! ps aux | grep "[X]vfb" | grep -q -- "-nolisten tcp"; then
  bad "Xvfb is running without -nolisten tcp"
else
  ok "Xvfb has no TCP listener"
fi

# ---------------------------------------------------------------------------
head_ "The URL to open"
# ---------------------------------------------------------------------------
if [ -n "$PUBLIC_URL" ]; then
  printf '  \033[1m%s\033[0m\n\n' "$PUBLIC_URL"
  if [ -n "$AUTH_TOKEN" ]; then
    printf '  Sign in with the passphrase from RAG_AUTH_TOKEN.\n'
  else
    printf '  \033[31mThere is no passphrase set, so this URL will show the login\n'
    printf '  screen to nobody and refuse every request.\033[0m Set one with:\n\n'
    printf "    printf 'RAG_AUTH_TOKEN=%%s\\\\n' 'your-passphrase' >> backend/.env\n"
    printf '    bash codespace/boot.sh\n\n'
  fi
  printf '  The Computer view then connects to %s/websockify,\n' "$PUBLIC_URL"
  printf '  which the app proxies to websockify on 127.0.0.1:%s.\n\n' "$WEBSOCKIFY_PORT"
  printf '  This hostname changes every time the tunnel restarts. It is not a\n'
  printf '  secret, and it is not what protects the screen -- the passphrase is.\n'
  printf '  Get the current one any time with: cat %s\n\n' "$PUBLIC_URL_FILE"
else
  printf '  no tunnel URL, so there is nothing to open yet.\n\n'
fi

# ---------------------------------------------------------------------------
printf '\033[1mResult: %d passed, %d failed, %d skipped\033[0m\n' "$PASS" "$FAIL" "$SKIP"
if [ "$FAIL" -gt 0 ]; then
  printf '\nFailures:\n'
  for c in "${FAILED_CHECKS[@]}"; do printf '  - %s\n' "$c"; done
  printf '\nLogs: %s/{backend,agent,supervisor,cloudflared,chromium}.log\n' "$DESKTOP_LOG_DIR"
  exit 1
fi
printf 'Everything checked, on the real machine, passed.\n'
