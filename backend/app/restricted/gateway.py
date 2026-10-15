"""The enforcement point: an allowlisting forward proxy.

A URL check inside the browser is not a boundary -- the same destination can be
reached by a shell, by a second browser, or by a raw socket.  What makes an
allowlist real is that *every* outbound connection has to come through one place
that refuses what is not approved, and that nothing else can open a socket at
all.  This is that place.

The gateway is an HTTP proxy in both of its modes:

* ``CONNECT host:port`` -- what an https site and a real browser use.  The host
  and port are screened, then a byte tunnel is opened to the screened address.
* an absolute-form request (``GET http://host/path``) -- what a plain fetcher
  uses.  Same screen, then the request is forwarded and the reply relayed.

A request that is not one of those, or names a destination the policy refuses,
gets a proxy error and no connection.  The check is done here rather than only
in the caller because the caller is application code the agent can influence;
this process is not.

The gateway is only half of the boundary, and the honest half to state is the
physical one: it must be the *only* egress.  The sandbox that runs the browser
and the ordinary-agent shell is placed in a network namespace whose firewall
drops every packet that is not to this socket (see ``codespace/restricted-sandbox.sh``).
Without that, a process that ignores its proxy setting still reaches the net.
With it, the browser's own URL check is defence in depth, not the boundary.
"""

from __future__ import annotations

import argparse
import select
import socket
import socketserver
import sys
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple
from urllib.parse import urlsplit

from .guard import Guard
from .policy import Policy, default_allowlist_path


@dataclass
class Target:
    """The screened destination of one proxy request."""

    allowed: bool
    host: str
    port: int
    addresses: Tuple[str, ...] = ()
    reason: str = ""

    def __bool__(self) -> bool:
        return self.allowed


def parse_request_line(line: str) -> Optional[Tuple[str, str]]:
    """(method, target) from an HTTP request line, or None when malformed."""
    parts = line.strip().split()
    if len(parts) < 2:
        return None
    return parts[0].upper(), parts[1]


#: Methods a browsing gateway needs.  Anything else is refused rather than
#: guessed at, so a method the proxy does not understand cannot become one it
#: half-forwards.
ALLOWED_METHODS = frozenset({"GET", "HEAD", "POST", "CONNECT"})


class Gateway:
    """The screening core, kept apart from the sockets so it can be tested.

    ``connect`` is what opens the upstream socket.  It is injectable for the
    same reason the resolver is: the *decision* is what the tests exercise for
    real, and the socket can be a stub that reaches an in-process test origin.
    The default connects to a screened address, never to the name.
    """

    def __init__(
        self,
        policy: Policy,
        *,
        guard: Optional[Guard] = None,
        connect: Optional[Callable[[str, int, Tuple[str, ...]], socket.socket]] = None,
    ):
        self.policy = policy
        self.guard = guard or Guard()
        self._connect = connect or self._default_connect

    @staticmethod
    def _default_connect(host: str, port: int, addresses: Tuple[str, ...]) -> socket.socket:
        # Connect to an address the guard actually screened, not to the name:
        # resolving twice would leave a window in which DNS answers differently.
        last: Optional[Exception] = None
        for addr in addresses or (host,):
            try:
                sock = socket.create_connection((addr, port), timeout=15)
                return sock
            except OSError as exc:  # pragma: no cover - exercised only live
                last = exc
        raise OSError(f"could not connect to {host}:{port}: {last}")

    def authorize(self, method: str, target: str) -> Target:
        """Screen one proxy request.  The single decision every path shares."""
        method = method.upper()
        if method not in ALLOWED_METHODS:
            return Target(False, "", 0, reason=f"method {method} is not allowed")
        if method == "CONNECT":
            host, port = self._split_authority(target)
        else:
            split = urlsplit(target)
            if not split.scheme:
                # An origin-form GET (no scheme) is not something this proxy
                # forwards: it carries no destination, and treating the Origin
                # header as one would be a guess.
                return Target(False, "", 0, reason="request is not absolute-form; no destination")
            if split.scheme.lower() not in ("http", "https"):
                return Target(False, split.hostname or "", 0, reason=f"scheme {split.scheme} is not http or https")
            host = split.hostname or ""
            try:
                port = split.port or (443 if split.scheme.lower() == "https" else 80)
            except ValueError:
                return Target(False, host, 0, reason="invalid port")
        if not host:
            return Target(False, "", 0, reason="no host in destination")

        # Two independent checks, in the order a reader expects: is it approved,
        # then is it even a public destination.
        decision = self.policy.allows(host, port)
        if not decision.allowed:
            return Target(False, host, port, reason=decision.reason)
        verdict = self.guard.check(f"http://{_authority(host, port)}")
        if not verdict.allowed:
            return Target(False, host, port, reason=verdict.reason)
        return Target(True, host, port, addresses=verdict.addresses, reason="allowed")

    @staticmethod
    def _split_authority(target: str) -> Tuple[str, int]:
        if target.startswith("["):
            # IPv6 literal authority: [::1]:443.  The guard will refuse a
            # private address; the split still has to be correct first.
            host, _, rest = target.partition("]")
            host = host[1:]
            port = int(rest[1:]) if rest.startswith(":") and rest[1:].isdigit() else 443
            return host, port
        host, _, port_text = target.partition(":")
        port = int(port_text) if port_text.isdigit() else 443
        return host, port


def _authority(host: str, port: int) -> str:
    # Bracket an IPv6 literal, leave a name alone.
    if ":" in host and not host.startswith("["):
        return f"[{host}]:{port}"
    return f"{host}:{port}"


class _Handler(socketserver.StreamRequestHandler):
    @property
    def gateway(self) -> Gateway:
        return self.server.gateway

    def handle(self) -> None:
        original_timeout = self.request.gettimeout()
        self.request.settimeout(30)
        try:
            line = self.rfile.readline(65536).decode("iso-8859-1")
        except OSError:
            return
        parsed = parse_request_line(line)
        if not parsed:
            self._refuse(400, "malformed request line")
            return
        method, target = parsed

        # Drain the headers so the request is fully read before anything else.
        headers = self._read_headers()
        decision = self.gateway.authorize(method, target)
        if not decision:
            # Logged with the destination the agent asked for, because the whole
            # value of a refusal is being able to read who asked for what.
            sys.stderr.write(f"[restricted] REFUSED {method} {target} -- {decision.reason}\n")
            sys.stderr.flush()
            self._refuse(403, decision.reason)
            return

        if method == "CONNECT":
            self._tunnel(decision)
        else:
            self._forward(method, target, headers, original_timeout)

    def _read_headers(self) -> List[str]:
        headers: List[str] = []
        while True:
            try:
                line = self.rfile.readline(65536).decode("iso-8859-1")
            except OSError:
                break
            if not line or line in ("\r\n", "\n"):
                break
            headers.append(line.rstrip("\r\n"))
        return headers

    def _refuse(self, code: int, reason: str) -> None:
        body = f"restricted browsing: {reason}\n".encode()
        try:
            self.wfile.write(
                b"HTTP/1.1 %d %s\r\nContent-Type: text/plain; charset=utf-8\r\n"
                b"Content-Length: %d\r\nConnection: close\r\n\r\n%s"
                % (code, b"Forbidden" if code == 403 else b"Bad Request", len(body), body)
            )
        except OSError:
            pass

    def _tunnel(self, decision: Target) -> None:
        try:
            upstream = self.gateway._connect(decision.host, decision.port, decision.addresses)
        except OSError as exc:
            sys.stderr.write(f"[restricted] upstream failed {decision.host}:{decision.port} -- {exc}\n")
            self._refuse(502, "upstream connection failed")
            return
        self.wfile.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        self.wfile.flush()
        client = self.connection
        upstream.setblocking(False)
        client.setblocking(False)
        try:
            self._pump(client, upstream)
        finally:
            try:
                upstream.close()
            except OSError:
                pass

    def _forward(self, method: str, target: str, headers: List[str], timeout) -> None:
        split = urlsplit(target)
        path = split.path or "/"
        if split.query:
            path += "?" + split.query
        decision = self.gateway.authorize(method, target)
        # authority was screened above; re-run cheaply is not needed, but the
        # decision is recomputed here so this method is safe to call alone.
        if not decision:
            self._refuse(403, decision.reason)
            return
        try:
            upstream = self.gateway._connect(decision.host, decision.port, decision.addresses)
        except OSError:
            self._refuse(502, "upstream connection failed")
            return
        upstream.settimeout(timeout or 30)
        request = [f"{method} {path} HTTP/1.1"]
        for header in headers:
            name = header.split(":", 1)[0].strip().lower()
            if name in ("proxy-connection", "connection", "host", "content-length"):
                continue
            request.append(header)
        request.append(f"Host: {split.hostname}")
        body = b""
        for header in headers:
            if header.lower().startswith("content-length:"):
                try:
                    length = int(header.split(":", 1)[1].strip())
                except ValueError:
                    length = 0
                if length > 0:
                    body = self.rfile.read(length)
                break
        request.append("Connection: close")
        if body:
            request.append(f"Content-Length: {len(body)}")
        payload = ("\r\n".join(request) + "\r\n\r\n").encode("iso-8859-1") + body
        try:
            upstream.sendall(payload)
            while True:
                chunk = upstream.recv(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
            self.wfile.flush()
        except OSError:
            pass
        finally:
            try:
                upstream.close()
            except OSError:
                pass

    @staticmethod
    def _pump(a: socket.socket, b: socket.socket) -> None:
        sockets = [a, b]
        while True:
            try:
                readable, _, errored = select.select(sockets, [], sockets, 60)
            except (OSError, ValueError):
                return
            if errored or not readable:
                return
            for source in readable:
                other = b if source is a else a
                try:
                    data = source.recv(65536)
                except OSError:
                    return
                if not data:
                    return
                try:
                    other.sendall(data)
                except OSError:
                    return


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address, gateway: Gateway):
        self.gateway = gateway
        super().__init__(address, _Handler)


def build_server(
    host: str,
    port: int,
    policy: Policy,
    *,
    guard: Optional[Guard] = None,
    connect: Optional[Callable[[str, int, Tuple[str, ...]], socket.socket]] = None,
) -> _Server:
    gateway = Gateway(policy, guard=guard, connect=connect)
    return _Server((host, port), gateway)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="restricted-browsing allowlisting gateway")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9111)
    parser.add_argument("--allowlist", default=default_allowlist_path())
    args = parser.parse_args(argv)

    policy = Policy(args.allowlist)
    describe = policy.describe()
    sys.stderr.write(
        f"[restricted] gateway on {args.host}:{args.port} allowlist={describe['path']} "
        f"rules={describe['rules']} rejected={len(describe['rejected'])}\n"
    )
    sys.stderr.flush()
    server = build_server(args.host, args.port, policy)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
