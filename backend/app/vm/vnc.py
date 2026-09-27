"""WebSocket bridge to the remote computer's real VNC server.

The Codespace exports its live X display with x11vnc (RFB on 127.0.0.1:5900)
and websockify turns that into a WebSocket on 127.0.0.1:6080.  This module is
the local equivalent of websockify for that one port: a plain byte relay
between a binary WebSocket and a TCP endpoint.  It is deliberately transparent
-- no frame inspection, no re-encoding, no thumbnailing -- so what the browser
draws is exactly what the remote machine's RFB server sends.

This relay is the *fallback* route.  In production the Computer view connects
straight to the Cloudflare-protected ``wss://computer.<domain>/websockify`` and
this path is not used at all; it exists so the screen still works during local
development and if the tunnel is down.

x11vnc is bound to 127.0.0.1, so this bridge never publishes the input surface
beyond the machine running the backend.  The only externally reachable route is
the authenticated Cloudflare hostname.
"""

from __future__ import annotations

import asyncio
import contextlib

_CONNECT_TIMEOUT = 6.0
_RELAY_CHUNK = 65536


async def tcp_listening(host: str, port: int, timeout: float = 1.0) -> bool:
    """True when something accepts a TCP connection on host:port."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
    except Exception:
        return False
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()
    return True


async def _pump_tcp_to_ws(reader: asyncio.StreamReader, websocket) -> None:
    while True:
        data = await reader.read(_RELAY_CHUNK)
        if not data:
            break
        await websocket.send_bytes(data)


async def _pump_ws_to_tcp(websocket, writer: asyncio.StreamWriter) -> None:
    while True:
        message = await websocket.receive()
        kind = message.get("type")
        if kind == "websocket.disconnect":
            break
        data = message.get("bytes")
        if not data:
            # Control frames (ping/pong) and stray text are not RFB; ignore them.
            continue
        writer.write(data)
        await writer.drain()


async def relay(websocket, host: str, port: int) -> bool:
    """Bridge one WebSocket to host:port.  Returns True if the RFB stream opened.

    On any failure the socket is closed, which is the client's cue to retry with
    backoff; keeping a half-open socket alive would only strand the UI in a
    "connected but blank" state.
    """
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=_CONNECT_TIMEOUT
        )
    except Exception:
        with contextlib.suppress(Exception):
            await websocket.close(code=1011)
        return False

    to_tcp = asyncio.create_task(_pump_ws_to_tcp(websocket, writer))
    to_ws = asyncio.create_task(_pump_tcp_to_ws(reader, websocket))
    try:
        done, pending = await asyncio.wait(
            {to_tcp, to_ws}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        for task in done:
            exc = task.exception()
            if exc is not None and not isinstance(exc, asyncio.CancelledError):
                raise exc
    except Exception:
        pass
    finally:
        for task in (to_tcp, to_ws):
            if not task.done():
                task.cancel()
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        with contextlib.suppress(Exception):
            await websocket.close()
    return True
