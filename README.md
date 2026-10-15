# RAG Agents

A multi-agent RAG system with a **Computer** the agents — and you — can actually
use: a real Google Chrome on a real Linux desktop, in a real browser tab, driven
by your own mouse and keyboard.

The Computer runs in a **GitHub Codespace** and reaches you through a
**Cloudflare Worker** that signs you in with Google and proxies a **Cloudflare
Quick Tunnel** behind it. There is no VM, no hypervisor, and no screenshot loop
in the path.

## What the Computer is

The Computer view is noVNC showing the live framebuffer of a real Chrome window
on a real X display, in a Codespace. When you type, Chrome types. When you
scroll, Chrome scrolls. What you see is the machine's actual screen state, and
the same machine is what the agents drive through their tools.

```
RAG Agents ──▶ Computer ──▶ Live Screen  (noVNC, the real framebuffer)
                  ├─▶ Browser    agent-driven, Chrome DevTools Protocol
                  ├─▶ Files      the machine's real filesystem
                  └─▶ Terminal   a real shell on the machine
```

- **Live screen**: `/websockify` on the tunnel's own origin, after the app has
  checked your session.
- **Browser / Files / Terminal**: the same machine, reached over HTTP on
  loopback by `backend/vm_agent/daemon.py`.
- **Profile**: `/workspaces/chrome-profile`, on the persistent volume, so the
  browser session survives restarts.

There is no recording, no replay, and no stand-in browser. The framebuffer never
passes through the model.

## Running it

### In a Codespace (the real setup)

```bash
codespace/boot.sh          # display, screen, tunnel, backend, agent
```

`boot.sh` prints the `https://<random>.trycloudflare.com` URL; that is the
backend origin, not the front door. Point the Worker's `BACKEND_ORIGIN` at it
(see below). The public entry point is the stable Worker origin,
`https://rag-agents.<account>.workers.dev`.

**Sign-in is Google.** The Worker runs the OIDC flow, stores the session in D1,
and only then proxies to the tunnel. Keep the tunnel URL private anyway — with
`RAG_INTERNAL_TOKEN` set, a direct caller without the token is refused, but there
is no reason to publish it.

Full walkthrough, architecture notes and troubleshooting in
[`codespace/README.md`](codespace/README.md).

### The Worker (the sign-in edge)

The stable origin is a Cloudflare Worker (`worker/`) that serves the built
frontend and owns authentication at the edge:

- `GET /auth/login` → Google, with scopes `openid email profile` only (so sign-in
  is independent of any Gmail authorization), plus PKCE, `state` and `nonce`.
- `GET /auth/callback` → verifies the ID token against Google's JWKS, upserts the
  user, mints a session in D1 and sets an HttpOnly `rag_session` cookie.
- `GET /auth/session`, `POST /auth/logout` → session state and sign-out.
- `POST`/`GET /internal/*` → a token-guarded API (same `x-rag-internal-token`)
  for the backend and the scheduler: the routine store and the manual scheduler
  tick. No user session reaches it.
- Every other path is proxied to `BACKEND_ORIGIN` with a per-request
  `x-rag-internal-token` and the signed-in identity added as headers. The backend
  trusts those only when the token matches its `RAG_INTERNAL_TOKEN`.
- A **Cron Trigger** (`* * * * *`, in `wrangler.jsonc`) drives the `scheduled`
  handler: it materialises due routines, wakes the Codespace through the GitHub
  API when one is due and the backend is down, and dispatches pending runs to
  `POST /internal/routines/execute`. See *Recurring routines* below.

Deploy:

```bash
cd worker
npx wrangler d1 create rag-agents-auth          # put the id in wrangler.jsonc
npx wrangler d1 migrations apply rag-agents-auth --remote   # 0001 identity, 0002 routines
npx wrangler secret put GOOGLE_CLIENT_SECRET    # from the Google OAuth client
npx wrangler secret put SESSION_SECRET          # a random 32+ byte string
npx wrangler secret put RAG_INTERNAL_TOKEN      # must equal the backend's value
npx wrangler secret put GITHUB_TOKEN            # fine-grained PAT with Codespaces lifecycle, for routine wake
npx wrangler deploy
```

Set `GOOGLE_CLIENT_ID`, `BACKEND_ORIGIN` and `CODESPACE_NAME` in
`worker/wrangler.jsonc`, and register
`https://rag-agents.<account>.workers.dev/auth/callback` as an authorized
redirect URI on the Google OAuth client. Without `GITHUB_TOKEN`/`CODESPACE_NAME`
the scheduler still queues and dispatches routines; it just cannot boot a stopped
Codespace, so it leaves due runs queued until the machine is up.

### Locally (development)

```bash
# backend
cd backend
python -m venv .venv && .venv\Scripts\activate     # or: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                                # add provider keys
python -m uvicorn app.main:app --reload --port 8000

# frontend (second terminal)
cd Frontend
npm install
npm run dev
```

With no tunnel running, `/screen/config` returns `mode: "bridge"` and the viewer
talks to the backend's own relay at `/ws/screen`. That is the development path
and needs no Cloudflare. The badge in the Computer view shows which route is live.

Locally, sign-in runs against the backend directly: the Vite dev server proxies
`/auth` to `uvicorn`, which performs the same Google OIDC flow and sets the
`rag_session` cookie. Set `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` in
`backend/.env` and register `http://localhost:8000/auth/callback` as a redirect
URI on the same OAuth client. With no Google client configured the app shows the
sign-in screen and cannot proceed.

Checks:

```bash
cd backend && python -m tests.run        # backend contract + isolation tests
cd Frontend && npm run build             # typecheck is part of the build
```

## Layout

```
backend/
  app/
    main.py             HTTP + WebSocket API, and the /screen/* routes
    auth/               Google OIDC, hashed sessions, and the gateway check
    config.py           settings, incl. where the tunnel URL is published
    agents/             commander, navigator, workers
    providers/          LLM providers and the router
    tools/              tool registry, executor, WorkspaceClient
    restricted/         ordinary-agent web access: allowlist, guard, gateway
    routines/           routine tools + edge client (create/list/pause/...)
    email/              the Gmail connection lifecycle (refresh, revoke, status),
                        plus the owner-only bot sender and notification tools
    vm/websockify_proxy.py  the /websockify -> :6080 relay
    vm/vnc.py           the fallback RFB-over-WebSocket relay
    workspace/          WorkspaceManager: the remote machine's lifecycle
  vm_agent/daemon.py    the agent on the machine: browser, files, terminal
  tests/                run with `python -m tests.run`
  _vm-archive/          the old local QEMU VM, preserved and ignored
Frontend/               Vite + React app
worker/                 the Cloudflare Worker: sign-in edge, D1 sessions, proxy,
                        the routine store + Cron Trigger scheduler
codespace/              the Codespace stack: display, screen, tunnel, boot
                        + the restricted-browsing sandbox (restricted-*.sh)
.devcontainer/          container definition and private port forwarding
```

## How the screen is protected

The live screen is a view of a signed-in browser, so the access control is part
of the product rather than optional setup:

- `x11vnc` binds `127.0.0.1:5900` only.
- `websockify` binds `127.0.0.1:6080` only.
- `Xvfb` runs with `-nolisten tcp`.
- The Quick Tunnel publishes **only** port 8000. It never points at websockify, so
  there is no route to the framebuffer that skips the app.
- The tunnel connection is outbound; nothing is opened on the machine.
- `/websockify`, `/ws/screen` and `/ws/{user}` all require a session, and the
  tunnel URL is *not* treated as a credential — a `trycloudflare.com` hostname
  turns up in DNS and logs, so the Google sign-in at the Worker is what actually
  gates the screen.
- GitHub's forwarded ports stay private, for debugging only.
- `backend/tests/test_screen_isolation.py` fails the build if any of that stops
  being true, and `test_auth.py` / `test_websockify_proxy.py` fail it if the gate
  or the relay stops working. `codespace/verify.sh` then re-checks all of it
  against the live machine, because a test that only ever talks to a mock cannot
  catch a port that is bound to the wrong interface.

## The two routes the screen can take

| Route | When | Path |
| --- | --- | --- |
| **Quick tunnel** | a tunnel URL is published | browser → tunnel → app `/websockify` → websockify :6080 |
| **Local relay** | no tunnel running | browser → app `/ws/screen` → x11vnc :5900 |

Both require the same session. The app and the screen share one origin on
purpose, which is what lets the session cookie ride along with the WebSocket
upgrade with no CORS handling at all.

## Restricted browsing for ordinary agents

The computer-control browser is the signed-in personal Chrome — a model clicks
pixels in the user's session. Ordinary agents (commander, workers, email,
summary) must not touch it. They browse through `browse`, a *separate* headless
Chrome with its own profile directory and its own port, and every way they can
reach the network is checked against an owner-controlled allowlist:

- **The allowlist** lives at `/etc/rag-restricted/allowlist` — root-owned, mode
  `0644`, so the unprivileged agent can read it and cannot rewrite it. It is
  edited only by the owner: `sudo codespace/restricted-policy.sh add example.com`.
  Default-deny: an empty file allows nothing.
- **The gateway** (`app/restricted/gateway.py`) is an allowlisting forward proxy.
  It screens `CONNECT` and absolute-form requests, and it is the only thing that
  opens an outbound socket.
- **The sandbox** (`codespace/restricted-sandbox.sh`) is what makes the allowlist
  a boundary. The browser and the ordinary-agent shell run in a Docker network
  with **no external route**; the gateway is the only service with one. So
  Chrome ignoring its `--proxy-server`, or a `curl` from the shell, cannot reach
  anything the policy refuses. The script *tests* this after starting — it tries a
  raw socket to a public address from inside each container and fails the deploy
  if it connects.
- **Fail closed.** `browse`, `fetch_url`, `web_search` and `shell` refuse when the
  restricted service is unreachable, and `shell` refuses outright until a
  sandboxed shell is deployed (`RESTRICTED_SHELL_URL`), because a shell on the
  host is a `curl` around the gateway. The unrestricted `browser` tool is removed
  from the ordinary-agent registry entirely — there is no second door.

Turn it on in `backend/.env`:

```
RESTRICTED_BROWSING=1
RESTRICTED_ALLOWLIST_PATH=/etc/rag-restricted/allowlist
RESTRICTED_BROWSER_URL=http://127.0.0.1:9110
RESTRICTED_GATEWAY_URL=http://127.0.0.1:9111
RESTRICTED_SHELL_URL=http://127.0.0.1:9112
```

then `codespace/restricted-sandbox.sh up`. The computer-control subsystem is
untouched: same browser, same profile, same loop, same controller.

## Connecting a Gmail mailbox

Sign-in and mailbox access are two separate grants. Logging in asks Google only
for `openid email profile`; connecting a mailbox is a distinct consent, offered
from Settings, that adds exactly `gmail.send` and `gmail.readonly`. Nothing else
is requested — no Drive scope, and no Gmail modify/compose/delete scope — so a
user who never connects a mailbox still signs in and uses everything else, and
the mailbox is reported as simply *not connected*.

- **Bound to the same identity.** The mailbox consent is checked against the
  signed-in session: authorizing a *different* Google account is rejected with
  `?gmail=wrong_account` and nothing is stored. `Reconnect` re-runs consent with
  `prompt=consent`, so a revoked grant always re-prompts.
- **Tokens are secrets at rest.** The refresh token (and the short-lived access
  token) are encrypted with AES-GCM under `APP_ENCRYPTION_KEY`, a fresh random
  nonce per value, and fail closed when the key is missing. They never reach the
  browser, a model prompt or a log line; the only mailbox facts the API returns
  are the address and a coarse status.
- **Refresh, revocation and reconnection.** When an access token nears expiry it
  is refreshed with the stored refresh token. A revoked/expired grant comes back
  from Google as `invalid_grant`; that is treated as *permanent* — the connection
  is marked `revoked`, the user is told to reconnect, and **no further refresh is
  attempted**. A transient failure (5xx, network) is retryable and does not
  discard the connection. `Disconnect` revokes the grant at Google (best-effort)
  and removes the local record either way.
- **Where the state lives.** The connection is owned by the backend (SQLite,
  keyed by the signed-in user). The Worker's `/auth/session` deliberately reports
  the mailbox as `unknown`; the UI reads `GET /auth/gmail/status` — proxied to
  the backend, which is the only thing that can say whether a grant is live.
- **Plus addressing.** A `user+alias@gmail.com` address is a different *sender*,
  not a narrower API permission: the read-only scope still covers the whole
  mailbox. The product does not claim otherwise.

This phase is the connection and its lifecycle only. Sending mail — and only to
the owner — is a separate feature with its own dedicated sender; see *Owner
notifications* below. Reading the inbox and the incoming-reply workflow are not
implemented.

## Owner notifications (an agent emailing its owner)

An agent may send a notification email to **its own owner and nobody else** — say,
"the nightly report is ready". The recipient and the sender are decided by the
backend, not the model.

- **The sender is a dedicated bot mailbox.** Notifications are sent from a
  bot-owned account configured on the server (`BOT_SENDER_*` / `BOT_SMTP_*`, see
  `backend/.env.example`), *not* from the owner's connected Gmail. The owner's
  mailbox is a personal credential; using it would let an assistant send mail as
  the user. The server holds the bot's refresh token (or SMTP app password)
  server-side, never in the frontend, encrypted at rest where possible. If no
  sender is configured the tools **fail closed** with a clear refusal — they never
  fall back to the owner's Gmail or any other path.
- **The recipient comes from the verified session.** It is the owner's verified
  email read from the user row the session resolved to. It is never taken from a
  tool argument, a message body, a fetched page or another agent's output, so a
  prompt injection cannot redirect a message. If the owner is unverified, has no
  address, or is not bound to the turn, the send is refused.
- **The model can only write the subject and body.** The three tools are
  `draft_owner_notification(subject, body)`,
  `update_owner_notification(draft_id, subject, body)` and
  `send_owner_notification(draft_id)`. Their schemas have **no recipient field**,
  and a call that tries to set a recipient, Cc/Bcc or From/Reply-To is refused
  outright. A draft id is unpredictable, and every read, edit and send is checked
  against the bound owner, so one user cannot touch another's draft.
- **One delivery per draft.** A draft is claimed atomically before the provider
  is called, so a duplicate or concurrent send cannot deliver twice. There are
  subject/body size limits and a per-owner hourly rate limit. A notification is
  reported sent only after the provider confirms it; the tools never claim a
  draft is a delivery.
- **Owner-only policy in the prompts.** The rule ("notifications are owner-only;
  you may not choose or change the recipient or sender; never claim a send that
  was not confirmed") is written into the trusted system prompts of the Commander,
  the workers, routines and summaries — not into anything a user or a page can
  edit.
- **Audit, scoped per owner.** Each attempt records the owner, the draft, the
  resolved recipient and sender, the provider and the outcome. The audit never
  stores a token, a header, or the message body, and one owner cannot read
  another's history.

The mail provider is mocked in the tests (`test_notification_owner_only`): no
real message is ever sent and no Google endpoint is contacted.

## Recurring routines for ordinary agents

An ordinary agent can create routines that fire later and keep firing — "every
weekday at 09:00 Paris time, summarise the inbox" — and they fire whether or not
the Codespace is running. The authority for *which* routine is due lives at the
edge, in the Worker's D1 database, not on the machine that can be shut down.

- **The tools.** `get_current_time`, `create_routine`, `list_routines`,
  `get_routine`, `update_routine`, `pause_routine`, `resume_routine`,
  `delete_routine`, `get_routine_run_status`. They are in the ordinary-agent
  registry and are owned by the signed-in user: `owner_sub` comes from the bound
  request identity (`app/routines/context.py`), never from a tool argument, so a
  model cannot create or read another user's routine.
- **Time and recurrence.** The authoritative clock is the server's, not the
  model's guess. Timestamps are stored UTC; a recurrence carries an explicit IANA
  timezone. Daily/weekly/monthly recurrences are resolved against that zone with
  fixed DST rules: a skipped wall time runs at the first existing instant after
  it, a repeated one at the earlier occurrence, and a monthly day that a short
  month lacks clamps to that month's last day. A materially ambiguous request is
  refused back to the model as a question rather than guessed.
- **Durable execution.** The Cron Trigger materialises due routines into run
  rows *before* any dispatch, with an idempotency key, a lease and bounded
  retries. A run moves through queued → running → completed (or delayed /
  rejected / failed); a rate limit or an outage becomes *delayed* with backoff,
  not a hot retry; a run whose lease expired is reclaimed rather than assumed
  done. The backend's `POST /internal/routines/execute` is idempotent: a
  re-dispatch of the same run returns the first result, and a run already in
  flight is reported retryable instead of executing twice.
- **The Codespace is not required to be up.** When a routine is due and the
  backend is unreachable, the scheduler queues the run, wakes the Codespace
  through the GitHub API (`GITHUB_TOKEN` + `CODESPACE_NAME`) and waits for a real
  `/health` before dispatching. With no lifecycle configured it simply leaves the
  run queued until the machine is up — never a fake keep-alive.
- **Boundary.** Routines are ordinary, non-computer tasks only. The routine
  executor has no computer-control browser attached, so a routine can never click
  the user's signed-in Chrome or reach the shared desktop, and it cannot bypass
  the deferred computer-task approval.
- **Passive summaries** reuse the same durable jobs: a summary is scheduled only
  after the conversation's quiet period, carries a transcript high-watermark so a
  stale job cannot overwrite fresher activity, and is retained (not dropped) when
  the model provider is out of quota.

The backend talks to the edge with `WORKER_ORIGIN` (falls back to
`PUBLIC_BASE_URL`) and the shared `RAG_INTERNAL_TOKEN`. Tests: the Worker suite
(`worker/`, `npx vitest run`) covers recurrence, the run ledger, the lifecycle
gate and the scheduler; the backend's `test_routines_tools.py` and
`test_routines_execute.py` cover the tools and the dispatch seam.

## Status

Verified locally: backend imports, all routes respond, the frontend typechecks
and builds, `/websockify` relays binary frames in both directions behind a
session, unauthenticated HTTP and WebSocket access is refused, the tunnel script
finds the URL cloudflared prints and follows a restart, the RFB relay
round-trips bytes in both directions, the RFB handshake in the live verifier is
byte-correct against a canned x11vnc, and the isolation checks pass.

- **Phase 1–3:** deployed, with tests covering auth/ownership/isolation, agent
  routing, history, memory and passive summaries.
- **Phase 4:** restricted browsing is wired: the registry offers only `browse`
  for ordinary agents, the guard enforces the allowlist, the gateway is the sole
  egress, and the sandbox is the honest boundary; the computer-control subsystem
  is untouched by these changes.
- **Phase 5:** recurring routines complete (tools + dispatch seam) and the
  Worker-side authority is written. The scheduled cron and the Codespace-wake
  paths are mock-tested; real verification needs `GITHUB_TOKEN`, a Codespace and
  applying `worker/migrations/0002_routines.sql` to the remote D1, plus deploying
  the Worker with the new secrets/vars.
- **Phase 6:** the Gmail connection is complete and tested end to end locally
  (separate consent, identity binding, encrypted tokens, refresh, revocation as
  permanent, transient retry, disconnect-with-revoke, and a real status in the
  UI). Verifying it against Google needs a real `GOOGLE_CLIENT_ID` /
  `GOOGLE_CLIENT_SECRET`, the redirect URIs registered, and the Gmail scopes
  enabled on that OAuth client; the token exchange is stubbed in the offline
  tests.
- **Phase 7:** owner-only notifications are complete and tested locally against a
  mocked provider (`test_notification_owner_only`): the recipient is the verified
  owner and the sender a dedicated bot mailbox, recipient/sender cannot be set by
  a tool call, cross-owner access is refused, an unbound owner or unconfigured
  sender fails closed, a draft is delivered once, and the audit is
  owner-isolated and secret-free. Sending a real message needs an admin to
  authorize a bot-owned mailbox and set `BOT_SENDER_*` / `BOT_SMTP_*`; no real
  email is sent by the tests.

Not yet verified: anything that needs the real machine — Chrome rendering,
profile persistence across a Codespace rebuild, and latency. Those need the
Codespace. Real end-to-end of Codespace wake and the deployed routine store also
need a real Codespace + remote deploy.

`bash codespace/verify.sh`, run inside the Codespace, is what closes that gap.
It asserts the whole path on the real machine and prints the URL to open, so
there is one command to run rather than a checklist to remember.
