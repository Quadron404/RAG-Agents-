# RAG Agents

A multi-agent RAG system with a **Computer** the agents — and you — can actually
use: a real Google Chrome on a real Linux desktop, in a real browser tab, driven
by your own mouse and keyboard.

The Computer runs in a **GitHub Codespace** and reaches you through a
**Cloudflare Quick Tunnel** guarded by the app's own passphrase. There is no VM,
no hypervisor, and no screenshot loop in the path.

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

Add one [Codespaces secret](https://github.com/settings/codespaces):

| Secret | Value |
| --- | --- |
| `RAG_AUTH_TOKEN` | a long random passphrase, e.g. `openssl rand -base64 24` |

That is the whole setup — no Cloudflare account, no domain, no tunnel to create
and no access policy to write, because a quick tunnel needs none of them. Open the
Codespace, take the `https://<random>.trycloudflare.com` URL from the log, and
enter the passphrase.

Full walkthrough, architecture notes and troubleshooting in
[`codespace/README.md`](codespace/README.md).

### Locally (development)

```bash
# backend
cd backend
python -m venv .venv && .venv\Scripts\activate     # or: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                                # add provider keys + RAG_AUTH_TOKEN
python -m uvicorn app.main:app --reload --port 8000

# frontend (second terminal)
cd Frontend
npm install
npm run dev
```

With no tunnel running, `/screen/config` returns `mode: "bridge"` and the viewer
talks to the backend's own relay at `/ws/screen`. That is the development path
and needs no Cloudflare. The badge in the Computer view shows which route is live.

`RAG_AUTH_TOKEN` is not optional: with no passphrase the app refuses every route
and every WebSocket, so a forgotten secret stops the deployment rather than
publishing a signed-in browser.

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
    auth.py             the passphrase gate and its signed session cookie
    config.py           settings, incl. where the tunnel URL is published
    agents/             commander, navigator, workers
    providers/          LLM providers and the router
    tools/              tool registry, executor, WorkspaceClient
    vm/websockify_proxy.py  the /websockify -> :6080 relay
    vm/vnc.py           the fallback RFB-over-WebSocket relay
    workspace/          WorkspaceManager: the remote machine's lifecycle
  vm_agent/daemon.py    the agent on the machine: browser, files, terminal
  tests/                run with `python -m tests.run`
  _vm-archive/          the old local QEMU VM, preserved and ignored
Frontend/               Vite + React app
codespace/              the Codespace stack: display, screen, tunnel, boot
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
  turns up in DNS and logs, so the passphrase is what actually gates the screen.
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

## Status

Verified locally: backend imports, all routes respond, the frontend typechecks
and builds, `/websockify` relays binary frames in both directions behind a
session, unauthenticated HTTP and WebSocket access is refused, the tunnel script
finds the URL cloudflared prints and follows a restart, the RFB relay
round-trips bytes in both directions, the RFB handshake in the live verifier is
byte-correct against a canned x11vnc, and the isolation checks pass.

Not yet verified: anything that needs the real machine — Chrome rendering,
profile persistence across a Codespace rebuild, an end-to-end login through a
live Quick Tunnel, and latency. Those need the Codespace and a `RAG_AUTH_TOKEN`.

`bash codespace/verify.sh`, run inside the Codespace, is what closes that gap.
It asserts the whole path on the real machine and prints the URL to open, so
there is one command to run rather than a checklist to remember.
