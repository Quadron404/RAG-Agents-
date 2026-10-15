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

Access is gated by **Google sign-in at the Cloudflare Worker**, which sits in
front of the tunnel: the Worker runs the OIDC flow, keeps the session in D1, and
only then proxies to this app. The app itself no longer has a passphrase
(`RAG_AUTH_TOKEN` was removed); instead it trusts the Worker's signed-in identity
when the request carries `RAG_INTERNAL_TOKEN`, and refuses a direct caller who
has neither the token nor a session.

The tunnel hostname is therefore **not** the credential it once was. Even so, the
hostname appears in DNS, proxy logs and browser history — keep it private and
restart the tunnel to move it.

## First run

1. **Google is the only thing to configure.** Create an OAuth client in the
   Google Cloud Console and register
   `https://rag-agents.<account>.workers.dev/auth/callback` as a redirect URI,
   then set `GOOGLE_CLIENT_ID`/`BACKEND_ORIGIN` in `worker/wrangler.jsonc` and the
   Worker secrets (`GOOGLE_CLIENT_SECRET`, `SESSION_SECRET`,
   `RAG_INTERNAL_TOKEN`). The backend needs the same `RAG_INTERNAL_TOKEN`. No
   Cloudflare account, domain or tunnel to create — a quick tunnel needs none of
   them.

2. Open the Codespace. The devcontainer installs the stack and starts
   everything; the log ends with a status block like:

   ```
   [computer] display   up   (1365x768 on :99)
   [computer] chrome    up   profile: /workspaces/chrome-profile
   [computer] RFB       up   127.0.0.1:5900  (loopback only)
   [computer] noVNC     up   127.0.0.1:6080  (loopback only)
   [computer] backend   up   :8000
   [computer] public    https://witty-pandas-repeat-7x9k.trycloudflare.com  (upstream; put this in BACKEND_ORIGIN)
   [computer] screen    https://witty-pandas-repeat-7x9k.trycloudflare.com/websockify  (via the app, not exposed directly)
   ```

   Open the Worker origin (`https://rag-agents.<account>.workers.dev`) and sign in
   with Google; the tunnel URL above is only the Worker's upstream.

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

### Restricted browsing (ordinary agents)

The Computer browser above is the human's signed-in Chrome. Ordinary agents
(commander, workers, email, summary) get a separate one, and a network boundary
that the allowlist actually enforces:

| Script | Role |
| --- | --- |
| `restricted-policy.sh` | The owner's allowlist tool. Root-only; edits `/etc/rag-restricted/allowlist` (root-owned, mode 0644) so the unprivileged agent can read it and cannot rewrite it. `add`, `remove`, `list`, `test`. |
| `restricted-sandbox.sh` | Starts the three containers and the two networks — the browser and shell on a network with **no external route**, the gateway on the only one that has a route — then *proves* it by trying a raw socket out of each container and failing the deploy if it connects. |
| `restricted-compose.yml` | The network layout the script drives. |
| `restricted.Dockerfile` | The image: Chrome, Python, and the restricted services. |
| `restricted-dev.sh` | Runs the services on loopback for local work. **Not a boundary** — no firewall, so the allowlist is advisory. |

```bash
sudo codespace/restricted-policy.sh add example.com
codespace/restricted-sandbox.sh up        # builds, starts, verifies the boundary
```

Then set `RESTRICTED_BROWSING=1` and the four `RESTRICTED_*` URLs in
`backend/.env` (see `.env.example`). The computer-control browser, profile and
loop are untouched.


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
curl -s 127.0.0.1:8000/auth/session  | jq   # who am I to the backend right now
curl -s 127.0.0.1:8000/health        | jq

# raw noVNC, bypassing the app entirely -- the fastest way to separate
# "the screen is broken" from "the app is broken"
open http://127.0.0.1:6080/vnc.html
```

Logs are in `/tmp`: `supervisor.log`, `agent.log`, `chromium.log`,
`cloudflared.log`, `backend.log`, `frontend-build.log`.

The `/websockify` endpoint above needs a session; `/health` and `/auth/session`
are open on purpose so a probe can use them without one.

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

## Routine wake and the Codespace

An ordinary agent can create a routine ("every weekday at 09:00") that must fire
even if the Codespace is stopped. The Cron Trigger in the Worker does the wake:

- The Worker's scheduler materialises due runs in D1 and checks `/health` on the
  backend origin (`BACKEND_ORIGIN`). If it is not reachable, it calls the GitHub
  API to `start` the Codespace (`CODESPACE_NAME`) using a fine-grained PAT in
  `GITHUB_TOKEN`. It then polls `/health` until it answers `ok`, or times out.
- Without `GITHUB_TOKEN` or `CODESPACE_NAME` it simply does not start the
  Codespace and leaves the run queued until the machine is up — it never fakes
  a boot. The same `BACKEND_ORIGIN` must point at the backend the Codespace
  serves (the Quick Tunnel or the public backend).
- `boot.sh` already makes the backend serve `/health` (reports `agent` and
  `restricted.describe()`), so no extra wiring is needed on the Codespace side.
- The backend's `POST /internal/routines/execute` is the dispatch target: one
  run, one execution, retries retained with backoff, in-flight runs refused as
  retryable. Identity (`x-rag-internal-token`) is required and checked by the
  backend and by the `internal` Worker route.
- `worker/migrations/0002_routines.sql` creates `routines` and `routine_runs` in
  the edge D1; `wrangler d1 migrations apply ... --remote` must include it before
  or when deploying the new cron trigger. `worker/wrangler.jsonc` declares a
  `scheduled` handler with `triggers.crons = ["* * * * *"]`.

If you can only run locally, Codespace wake cannot be verified — it needs a real
Codespace and PAT. All scheduler paths that don't reach GitHub are exercised by
the Worker's Vitest suite (`codespace.ts`, `scheduler.ts`, `internal.ts`).

## Troubleshooting

Start with `bash codespace/verify.sh` — it names the failing step, which is
usually faster than reading logs by hand.

| Symptom | Likely cause |
| --- | --- |
| The app answers but every route is refused | A protected route needs either the Worker's `RAG_INTERNAL_TOKEN` or a valid session, so a 401/403 here means the request reached the backend directly without the Worker — a stale `uvicorn` from before the change, or a proxy in front. Check `backend.log` and restart with `codespace/boot.sh`. |
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
| The agent still behaves like the old code after a `git pull` | The running daemon was started before the pull, and a pull never reaches an already-running process. Confirm the checkout is the commit you expect (`git rev-parse HEAD`) and that nothing under `backend/app/computer/` changed since the agent started, then restart it with `bash codespace/boot.sh` (or let `supervise.sh` relaunch it). Until the process restarts, the fix is on disk but not in the running agent. |
