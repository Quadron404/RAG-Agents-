# The Computer, on a Codespace

The Computer view is a **real Google Chrome on a real Linux desktop**, running
inside your Codespace, and you drive it with your own mouse and keyboard
through noVNC. There is no screenshot loop, no recording, and no stand-in
browser anywhere in the path.

```
your browser
  │  wss://computer.<your-domain>/websockify
  ▼
Cloudflare edge  ──▶  Cloudflare Access  (who are you?)
  │
  ▼
cloudflared  ──▶  websockify 127.0.0.1:6080  ──▶  x11vnc 127.0.0.1:5900
                                                          │
                                                          ▼
                                              Xvfb :99 ─▶ Google Chrome
```

The agents' three apps (Browser, Files, Terminal) reach the same machine over
HTTP on loopback, through `backend/vm_agent/daemon.py`.

## The one rule

**Nothing is published except an authenticated Cloudflare hostname.** The VNC
and WebSocket ports bind to `127.0.0.1`, the tunnel connection is outbound, and
GitHub's forwarded ports stay private. If the Access policy is wrong, the screen
is reachable — so treat the Access policy as part of the product, not as
optional setup.

## First run

1. **Create the tunnel** in Cloudflare Zero Trust → Networks → Tunnels, and
   copy its token.
2. **Add two Codespaces secrets** (Settings → Codespaces → *your codespace* →
   Codespaces secrets):

   | Name | Value |
   | --- | --- |
   | `CF_TUNNEL_TOKEN` | the tunnel token from step 1 |
   | `COMPUTER_HOSTNAME` | e.g. `computer.example.com` |

   `COMPUTER_HOSTNAME` is the only place the domain is written down. The viewer
   URL (`wss://<host>/websockify`) and the generated tunnel config are both
   derived from it, so they cannot drift apart.

3. **Protect the hostname** with an Access policy — see below. Do this before
   sharing the link with anyone.
4. Open the Codespace. The devcontainer installs the stack and starts
   everything; the build log ends with a status block like:

   ```
   [computer] display   up   (1365x768 on :99)
   [computer] chrome    up   profile: /workspaces/chrome-profile
   [computer] RFB       up   127.0.0.1:5900  (loopback only)
   [computer] noVNC     up   127.0.0.1:6080  (loopback only)
   [computer] backend   up   :8000
   [computer] tunnel    started
   [computer] screen    https://computer.example.com/websockify
   ```

## The Access policy

The screen shows a signed-in browser on a real desktop. Configure it like the
sensitive application it is:

- **Who** — specific identities (email, email domain, or your IdP group), not
  "everyone".
- **How** — a second factor: OTP, or your IdP's own MFA.
- **Session** — a short idle timeout, around 30 minutes.

Then open the hostname in a private window and confirm you are challenged.
A screen reachable without a login is a live view of someone's browser.

### Why the app and screen share one hostname

The tunnel routes `^/websockify(/.*)?$` to websockify and **everything else** to
the backend on port 8000. That is deliberate. Access hands the browser a
first-party cookie for the hostname, so the noVNC WebSocket upgrade carries it
with no CORS handling at all. Put the screen on a separate hostname and it
becomes a cross-site request, which browsers increasingly answer by dropping the
cookie — the user gets bounced to the login page mid-session.

## The scripts

| Script | Role |
| --- | --- |
| `env.sh` | The one place that decides display, ports and profile. Every value is overridable from the environment, which is how the multi-user layout later gets one browser per user instead of a shared one. |
| `install.sh` | Installs Chrome, Xvfb, Fluxbox, x11vnc, websockify, noVNC, cloudflared and the Python deps. Run by the devcontainer; safe to re-run. |
| `start-computer.sh` | Brings up the display, Chrome, x11vnc and websockify. Run it directly when you just want a local screen. |
| `start-tunnel.sh` | Generates the tunnel config from `COMPUTER_HOSTNAME` and runs `cloudflared` with the token. |
| `supervise.sh` | Keeps the agent, websockify and tunnel alive. This is what the Codespace runs. |
| `boot.sh` | The Codespace entry point: build the UI, then the screen stack, then the backend. |

### Manual start

```bash
source codespace/env.sh

bash codespace/install.sh          # first run only
bash codespace/start-computer.sh   # display + Chrome + screen
bash codespace/start-tunnel.sh     # only once CF_TUNNEL_TOKEN is set
bash codespace/boot.sh              # or just do all of the above
```

Useful checks:

```bash
listening 5900     # x11vnc is up
listening 6080     # websockify is up
listening 9000     # the agent is up

curl -s 127.0.0.1:9000/display/status | jq
curl -s 127.0.0.1:8000/screen/config | jq      # mode and wsUrl the UI will use
curl -s 127.0.0.1:8000/health     | jq

# raw noVNC, bypassing the app entirely -- the fastest way to separate
# "the screen is broken" from "the app is broken"
open http://127.0.0.1:6080/vnc.html
```

Logs are in `/tmp`: `supervisor.log`, `agent.log`, `chromium.log`,
`cloudflared.log`, `backend.log`, `frontend-build.log`.

## The profile is the session

Chrome runs against `/workspaces/chrome-profile`, which lives on the persistent
Codespaces volume. That directory *is* the user's login state: cookies, local
storage, extensions, everything. Restarting the Codespace keeps you signed in;
rebuilding the container does not log you out. Do not point it at `/tmp` or
anywhere on the container filesystem.

## The fallback route

If `COMPUTER_HOSTNAME` is unset, `/screen/config` returns `mode: "bridge"` and
the viewer connects to the backend's own relay at `/ws/screen` instead. Same
protocol, one extra hop, and it works with no Cloudflare setup at all — which
makes it the right way to develop locally.

It is a development convenience and nothing more. That route is protected by the
app's own authentication, not by Access, so do not point production at it. The
badge in the top-left of the Computer view always says which one is live:
**Secure tunnel** or **Local relay**.

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| Screen stuck on "connecting" | The tunnel is up but the origin is not. Check `listening 6080`. |
| Bounced to the Access login repeatedly | Screen and app are on different hostnames, so the cookie is being dropped. |
| 502 from Cloudflare | The backend on 8000 is not running. Check `backend.log`. |
| `CF_TUNNEL_TOKEN is not set` | The secret is missing, or the Codespace was created before it was added — recreate or restart it. |
| Chrome logs out every rebuild | `CHROME_PROFILE` is pointing off the persistent volume. |
| Blank screen, everything "up" | Chrome may not have started. Check `chromium.log` and `/display/status`. |
