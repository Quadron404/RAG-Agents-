# The Computer, on a Codespace

The Computer view is a **real Google Chrome on a real Linux desktop**, running
inside your Codespace, and you drive it with your own mouse and keyboard
through noVNC. There is no screenshot loop, no recording, and no stand-in
browser anywhere in the path.

```
your browser
  │  https://<random>.trycloudflare.com/websockify
  ▼
Cloudflare edge          (a pipe, not a lock)
  │
  ▼
cloudflared  ──▶  RAG Agents app 127.0.0.1:8000
                      │  session cookie?  no → 1008, nothing moves
                      ▼  yes
                   websockify 127.0.0.1:6080  ──▶  x11vnc 127.0.0.1:5900
                                                            │
                                                            ▼
                                                Xvfb :99 ─▶ Google Chrome
```

The agents' three apps (Browser, Files, Terminal) reach the same machine over
HTTP on loopback, through `backend/vm_agent/daemon.py`.

## The one rule

**Only the app is published. Nothing else has an address.**

The Quick Tunnel points at port 8000 and nothing else. 5900 and 6080 bind
`127.0.0.1` and are never given a public port, so the framebuffer has no address
of its own — `/websockify` on the app is the only way to reach it.

There is no passphrase on top of that. The app used to sit behind one
(`RAG_AUTH_TOKEN` plus a login screen); it was removed at the owner's request,
and every route now serves whoever asks. That makes a quick tunnel's hostname
the credential, even though the hostname is **not** a secret in any technical
sense: it appears in DNS, in proxy logs and in browser history. Treat it as
password material, and restart the tunnel to revoke it.

## First run

1. **Nothing to configure.** No Cloudflare account, no domain, no tunnel to
   create, no access policy to write, and no passphrase to set — a quick tunnel
   needs none of them, and the app no longer asks for one.

2. Open the Codespace. The devcontainer installs the stack and starts
   everything; the log ends with a status block like:

   ```
   [computer] display   up   (1365x768 on :99)
   [computer] chrome    up   profile: /workspaces/chrome-profile
   [computer] RFB       up   127.0.0.1:5900  (loopback only)
   [computer] noVNC     up   127.0.0.1:6080  (loopback only)
   [computer] backend   up   :8000
   [computer] public    https://witty-pandas-repeat-7x9k.trycloudflare.com  (no sign-in)
   [computer] screen    https://witty-pandas-repeat-7x9k.trycloudflare.com/websockify  (via the app, not exposed directly)
   ```

   Open that URL and you are straight into a live, signed-in browser. So treat
   the URL like the passphrase it replaced: it is the screen's only key now, and
   restarting the tunnel is how you take that key back.

## The hostname changes; the app follows

A quick tunnel is assigned a random `trycloudflare.com` name every time it
starts, so there is nothing to configure and nothing to hardcode:

- `start-tunnel.sh` reads the URL cloudflared prints and writes it to
  `PUBLIC_URL_FILE` (by default `/tmp/ragdesktop/public-url`).
- `/screen/config` reads that file on every request and hands the current origin
  to the browser.
- The viewer re-reads it periodically, so a tunnel that restarts is picked up
  without a rebuild or a redeploy.

`/screen/config` also reports `publicUrlAgeSeconds`, so the UI can say a URL has
been dead for a while rather than just failing quietly.

The last URL wins, and the file is removed on exit — a stale hostname is worse
than none, because the app would advertise an address Cloudflare has forgotten.

## The scripts

| Script | Role |
| --- | --- |
| `env.sh` | The one place that decides display, ports and profile. Every value is overridable from the environment, which is how the multi-user layout later gets one browser per user instead of a shared one. |
| `install.sh` | Installs Chrome, Xvfb, Fluxbox, x11vnc, websockify, noVNC, cloudflared and the Python deps. Run by the devcontainer; safe to re-run. |
| `start-computer.sh` | Brings up the display, Chrome, x11vnc and websockify. Run it directly when you just want a local screen. |
| `start-tunnel.sh` | Runs `cloudflared tunnel --url http://127.0.0.1:8000` and records the URL it was given. |
| `supervise.sh` | Keeps the agent, websockify and tunnel alive. This is what the Codespace runs. |
| `boot.sh` | The Codespace entry point: build the UI, then the screen stack, then the backend. |
| `verify.sh` | Proves the whole path works on the real machine and prints the URL to open. Run this first when something looks broken. |

### Manual start

```bash
source codespace/env.sh

bash codespace/install.sh          # first run only
bash codespace/start-computer.sh   # display + Chrome + screen
bash codespace/start-tunnel.sh     # the public URL
bash codespace/boot.sh             # or just do all of the above
```

### Prove it works

```bash
bash codespace/verify.sh
```

This is the check to run before trusting a deployment, and the first thing to
run when something looks wrong. It starts whatever is missing, then asserts the
whole path end to end: Chrome is running with its normal UI (explicitly *not*
kiosk, which would hide the tab strip and address bar), `5900` and `6080` are
bound to loopback and nothing else, the tunnel really is a `trycloudflare`
origin, `/screen/config` is served over the tunnel to a request with no cookie,
and a real RFB handshake returns real framebuffer pixels through
`/websockify`. It finishes by printing the exact URL to open.

It reports `SKIP` separately from `PASS`, because "could not check" is not the
same as "fine" — a `SKIP` on the screen check means the check did not happen.

Useful checks:

```bash
listening 5900     # x11vnc is up
listening 6080     # websockify is up
listening 9000     # the agent is up

curl -s 127.0.0.1:9000/display/status | jq
curl -s 127.0.0.1:8000/auth/status  | jq   # is auth on, is this session good
curl -s 127.0.0.1:8000/health       | jq

# raw noVNC, bypassing the app entirely -- the fastest way to separate
# "the screen is broken" from "the app is broken"
open http://127.0.0.1:6080/vnc.html
```

Logs are in `/tmp`: `supervisor.log`, `agent.log`, `chromium.log`,
`cloudflared.log`, `backend.log`, `frontend-build.log`.

The `/websockify` and `/health` and `/auth/status` endpoints above need a cookie;
`/health` and `/auth/status` are open on purpose so a probe can use them.

## The profile is the session

Chrome runs against `/workspaces/chrome-profile`, which lives on the persistent
Codespaces volume. That directory *is* the user's login state: cookies, local
storage, extensions, everything. Restarting the Codespace keeps you signed in;
rebuilding the container does not log you out. Do not point it at `/tmp` or
anywhere on the container filesystem.

## The fallback route

If the tunnel is not up, `/screen/config` returns `mode: "bridge"` and the viewer
connects to the backend's own relay at `/ws/screen` instead. Same protocol, one
extra hop, and it needs no Cloudflare at all — which makes it the right way to
develop locally.

It is not a way around the lock: `/ws/screen` is session-gated exactly like
`/websockify`. The badge in the top-left of the Computer view always says which
route is live: **Quick tunnel** or **Local relay**.

| `vnc_port` | `5900` | x11vnc / RFB. `-localhost`, so no public port. |
| `websockify_port` | `6080` | the RFB→WebSocket hop the app's `/websockify` proxies to. Loopback only. |
| `backend_port` | `8000` | the app. Loopback only, and the *only* port the tunnel publishes. |
| `agent_port` | `9000` | the browser/files/terminal daemon. Loopback only. |

Nothing in that table may ever be a wildcard. Three of them were `0.0.0.0` at
one point and a live run caught it; `backend/tests/test_bind_mutations.py` now
mutates each bind back to a wildcard and fails if the suite stops noticing, so
the mistake cannot be reintroduced quietly.

## Troubleshooting

Start with `bash codespace/verify.sh` — it names the failing step, which is
usually faster than reading logs by hand.

| Symptom | Likely cause |
| --- | --- |
| The app answers but every route is refused | The app no longer has a passphrase, so this means something upstream is returning 401 — a stale `uvicorn` process from before the removal, or a proxy in front. Check `backend.log` and restart with `codespace/boot.sh`. |
| `mode: "bridge"` when you expected a tunnel | No URL in `$PUBLIC_URL_FILE`. Check `cloudflared.log`; the tunnel takes a few seconds to be assigned a hostname. |
| Screen stuck on "connecting" | The tunnel is up but the origin is not. Check `listening 6080`. |
| 502 from Cloudflare | The backend on 8000 is not running. Check `backend.log`. |
| `cloudflared: command not found` | Run `codespace/install.sh`, or use the Codespaces port forward — the screen still works over `/ws/screen`. |
| The app is not up, and `backend.log` shows `ModuleNotFoundError` | `install.sh` never completed. It now exits non-zero and says so; re-run `sudo bash codespace/install.sh`. `boot.sh` also picks `backend/.venv` if the deps are there. |
| `boot.sh` prints `FATAL: no python with uvicorn+fastapi` | Neither the venv nor system python3 has the requirements. `sudo bash codespace/install.sh`. |
| RFB is up but the Computer view cannot connect | websockify on 6080 is not running. It is supervised by both the daemon and `supervise.sh`; check `/display/status`, which reports `websockify` and its pid, and `/tmp/websockify.log`. |
| `/display/status` says `vnc: true` but nothing is viewable | `vnc` being true only means RFB is listening. Check `websockify` in the same payload — that was the exact shape of the 6080 failure. |
| Chrome logs out every rebuild | `CHROME_PROFILE` is pointing off the persistent volume. |
| Blank screen, everything "up" | Chrome may not have started. Check `chromium.log` and `/display/status`. |
