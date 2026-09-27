# RAG Agents

A multi-agent RAG system with a **Computer** the agents — and you — can actually
use: a real Google Chrome on a real Linux desktop, in a real browser tab, driven
by your own mouse and keyboard.

The Computer runs in a **GitHub Codespace** and reaches you through an
authenticated **Cloudflare Tunnel**. There is no VM, no hypervisor, and no
screenshot loop in the path.

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

- **Live screen**: `wss://computer.<your-domain>/websockify`, behind Cloudflare
  Access.
- **Browser / Files / Terminal**: the same machine, reached over HTTP on
  loopback by `backend/vm_agent/daemon.py`.
- **Profile**: `/workspaces/chrome-profile`, on the persistent volume, so the
  browser session survives restarts.

There is no recording, no replay, and no stand-in browser. The framebuffer never
passes through the model.

## Running it

### In a Codespace (the real setup)

The devcontainer installs everything and starts the stack. Add two
[Codespaces secrets](https://github.com/settings/codespaces) first:

| Secret | Value |
| --- | --- |
| `CF_TUNNEL_TOKEN` | your named Cloudflare Tunnel token |
| `COMPUTER_HOSTNAME` | e.g. `computer.example.com` |

Then protect the hostname with a Cloudflare Access policy, and open the
Codespace. Full walkthrough, architecture notes and troubleshooting in
[`codespace/README.md`](codespace/README.md).

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

With no `COMPUTER_HOSTNAME` set, `/screen/config` returns `mode: "bridge"` and
the viewer talks to the backend's own relay at `/ws/screen`. That is the
development path and needs no Cloudflare setup. The badge in the Computer view
shows which route is live.

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
    config.py           settings, incl. the derived screen URL
    agents/             commander, navigator, workers
    providers/          LLM providers and the router
    tools/              tool registry, executor, WorkspaceClient
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
- The tunnel connection is outbound; nothing is opened on the machine.
- GitHub's forwarded ports stay private, for debugging only.
- The only external route is a hostname behind Cloudflare Access.
- `backend/tests/test_screen_isolation.py` fails the build if any of that stops
  being true.

Quick tunnels (`trycloudflare.com`) are not used anywhere: those URLs are
public, change on every restart, and cannot be put behind Access.

## The three routes the screen can take

| Route | When | Protection |
| --- | --- | --- |
| **Secure tunnel** | `COMPUTER_HOSTNAME` set | Cloudflare Access |
| **Local relay** | no hostname set | the app's own auth; development only |
| `/ws/screen`** | used by the relay | never the production path |

The app and the screen are served from **one hostname** on purpose: the Access
cookie is then first-party for both, so the WebSocket upgrade needs no CORS
handling. Splitting them across hostnames makes the screen a cross-site request,
which browsers increasingly answer by dropping the cookie.

## Status

Verified locally: backend imports, all routes respond, the frontend typechecks
and builds, the RFB relay round-trips bytes in both directions, and the
isolation checks pass.

Not yet verified: anything that needs the real machine — Cloudflare Tunnel and
Access acceptance, Chrome rendering, profile persistence across a Codespace
rebuild, and latency. Those need the Codespace, a domain, and the tunnel token.
