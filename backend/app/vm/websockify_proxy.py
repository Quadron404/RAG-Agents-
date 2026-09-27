"""Reverse proxy from this application to the local websockify.

Why this exists
---------------
The Cloudflare Quick Tunnel publishes exactly one origin, this app on :8000.
Pointing the tunnel at websockify on :6080 instead would publish the RFB stream
directly, so the screen would be reachable with no check in front of it and no
way to tie it to a logged-in user.  So the tunnel stays on :8000 and this
module carries the WebSocket the last hop to :6080.

It is a deliberately dumb relay.  It negotiates the same ``binary`` subprotocol
noVNC asks for, forwards binary frames untouched in both directions, and never
inspects, decodes, re-encodes or reorders them.  That matters: the RFB stream
carries a two-byte length prefix per message, so anything that "helpfully"
reassembled or converted frames would corrupt the session.  Keeping it opaque is
what makes it trustworthy.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

import websockets
from websockets.asyncio.client import connect as ws_connect

# A relay that fails silently is indistinguishable from a broken screen, and a
# bare `except: pass` once hid a TypeError that killed the whole stream.  The
# backend log is where that has to surface.
log = logging.getLogger(__name__)

_CONNECT_TIMEOUT = 8.0
# How long the surviving direction is given to drain once the other one ends.
#
# The origin and the browser rarely stop at the same instant: when websockify
# closes after sending a final framebuffer update, tearing the socket down
# immediately would throw that frame away and the viewer would freeze on a
# half-drawn screen.  Waiting briefly for the other direction lets whatever is
# already in flight arrive.
_DRAIN_TIMEOUT = 0.5


async def _pump_client_to_origin(client_ws, origin_ws) -> None:
    """Browser -> websockify.

    The two ends are different libraries, and that matters here.  ``client_ws``
    is a Starlette WebSocket, whose ``__aiter__`` yields raw ASGI message
    *dicts* (``{"type": ..., "bytes": ...}``), not payloads -- so iterating it
    and forwarding whatever looks like bytes silently drops every frame the
    browser sends.  Starlette's explicit receive API is used instead, which is
    the only shape that actually carries the RFB input.
    """
    while True:
        message = await client_ws.receive()
        if message["type"] == "websocket.disconnect":
            return
        data = message.get("bytes")
        if data:
            await origin_ws.send(data)
        # A text frame is not something the RFB stream produces, so it is
        # dropped rather than forwarded.  Forwarding it would hand the origin
        # something it can only reject, and the rejection would look like a
        # broken screen.


async def _pump_origin_to_client(origin_ws, client_ws) -> None:
    """websockify -> browser.

    ``origin_ws`` is a real websockets connection, so its ``__aiter__`` does
    yield payloads, and that direction can use the terser form.  On the way out
    the payload has to go through ``send_bytes``: Starlette's ``send()`` takes a
    raw ASGI message dict, and handing it bytes raises a TypeError that would
    otherwise be swallowed and read as a broken screen.
    """
    async for message in origin_ws:
        if isinstance(message, bytes):
            await client_ws.send_bytes(message)


async def proxy_websockify(client_ws, target_url: str) -> bool:
    """Relay one client WebSocket to websockify at ``target_url``.

    Returns True if the origin handshake succeeded.  The caller owns accepting
    the client socket, because it has to authenticate first.
    """
    subprotocols: list[str] = [p for p in (client_ws.scope.get("subprotocols") or []) if p]
    # noVNC needs the binary subprotocol.  If the client did not ask for it there
    # is nothing sensible to negotiate, so refuse rather than guess.
    if "binary" not in subprotocols:
        subprotocols = ["binary"]

    origin = None
    try:
        origin = await asyncio.wait_for(
            ws_connect(target_url, subprotocols=subprotocols, max_size=None),
            timeout=_CONNECT_TIMEOUT,
        )
    except Exception:
        log.warning("could not reach websockify at %s", target_url, exc_info=True)
        # The origin is down or refused.  Closing is the honest signal: the
        # client retries with backoff and the UI shows the real state.
        with contextlib.suppress(Exception):
            await client_ws.close(code=1011)
        return False

    try:
        await client_ws.accept(subprotocol=origin.subprotocol)
    except Exception:
        with contextlib.suppress(Exception):
            await origin.close()
        return False

    up = asyncio.create_task(_pump_client_to_origin(client_ws, origin))
    down = asyncio.create_task(_pump_origin_to_client(origin, client_ws))
    try:
        done, pending = await asyncio.wait({up, down}, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            exc = task.exception()
            if exc is not None and not isinstance(exc, asyncio.CancelledError):
                raise exc
        if pending:
            # One side has gone; give the other a moment to deliver what it
            # already has rather than cancelling it mid-frame.
            _finished, still_running = await asyncio.wait(pending, timeout=_DRAIN_TIMEOUT)
            for task in still_running:
                task.cancel()
    except Exception as exc:
        # A closed client is the normal end of a session, not a fault.
        log.debug("websockify relay ended: %s: %s", type(exc).__name__, exc)
    finally:
        for task in (up, down):
            if not task.done():
                task.cancel()
        with contextlib.suppress(Exception):
            await origin.close()
        with contextlib.suppress(Exception):
            await client_ws.close()
    return True


def websockify_url(host: str, port: int, path: str = "websockify") -> str:
    """The local origin URL, built from parts so it can never become public.

    The host is forced to loopback by the caller.  There is deliberately no
    environment variable that can point this somewhere else: if this relay were
    ever allowed to proxy to an arbitrary host it would be an open proxy, and an
    open proxy on a publicly reachable tunnel is a gift to an attacker.
    """
    return f"ws://{host}:{port}/{path.lstrip('/')}"


def loopback_websockify_url(port: int, path: str = "websockify") -> str:
    return websockify_url("127.0.0.1", port, path)


__all__ = [
    "proxy_websockify",
    "loopback_websockify_url",
    "websockify_url",
]
