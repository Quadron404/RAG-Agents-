"""The enforcement point: an allowlisting forward proxy.

The connection to the upstream is *mocked* here -- a stub dials a local origin
server instead of the real destination -- because a unit test must not depend on
the internet.  Everything else is real: the policy, the guard, the request
parsing, the refusal codes and the byte relay are the code that runs in the
sandbox.  What the stub proves is that the proxy asks for a destination, decides,
and only then connects; what the refusals prove is that a destination the policy
or the guard rejects never reaches the connect call at all.
"""
import http.client
import os
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND_DIR)

from app.restricted.gateway import Gateway, build_server
from app.restricted.guard import Guard
from app.restricted.policy import Policy

failures = []


def check(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"ok   {label}")
    else:
        failures.append(label)
        print(f"FAIL {label} {detail}")


RESOLVE = {
    "example.com": ["93.184.216.34"],
    "docs.example.com": ["93.184.216.35"],
    "evil.example.net": ["93.184.216.20"],
    "rebind.example": ["93.184.216.34", "127.0.0.1"],
}


def resolve(host):
    if host not in RESOLVE:
        raise OSError(f"no such host: {host}")
    return RESOLVE[host]


policy = Policy(text="example.com\n*.example.com\n")
guard = Guard(resolve=resolve)

# --- the decision, without any sockets -----------------------------------
gateway = Gateway(policy, guard=guard)

check("CONNECT to an approved host is authorized", gateway.authorize("CONNECT", "example.com:443").allowed is True)
check("CONNECT to a subdomain of an approved host is authorized",
      gateway.authorize("CONNECT", "docs.example.com:443").allowed is True)
check("CONNECT to an unlisted host is refused", gateway.authorize("CONNECT", "evil.example.net:443").allowed is False)
check("CONNECT to a loopback address is refused", gateway.authorize("CONNECT", "127.0.0.1:80").allowed is False)
check("CONNECT to a rebinding name is refused", gateway.authorize("CONNECT", "rebind.example:443").allowed is False)
check("CONNECT to an unlisted port is refused", gateway.authorize("CONNECT", "example.com:22").allowed is False)

check("an absolute-form GET to an approved host is authorized",
      gateway.authorize("GET", "http://example.com/x").allowed is True)
check("an absolute-form GET to an unlisted host is refused",
      gateway.authorize("GET", "http://evil.example.net/x").allowed is False)
check("a relative request has no destination and is refused",
      gateway.authorize("GET", "/local/path").allowed is False)
check("a non-http scheme is refused", gateway.authorize("GET", "ftp://example.com/f").allowed is False)
check("a method the proxy does not need is refused", gateway.authorize("DELETE", "http://example.com/").allowed is False)

# A refusal must name why, so an operator reading the log can act on it.
refusal = gateway.authorize("CONNECT", "evil.example.net:443")
check("a refusal explains itself", bool(refusal.reason), refusal.reason)

# --- a real round trip, with a stubbed upstream --------------------------
class _Origin(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        body = b"hello from origin"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


origin = ThreadingHTTPServer(("127.0.0.1", 0), _Origin)
origin_port = origin.server_address[1]
threading.Thread(target=origin.serve_forever, daemon=True).start()


def stub_connect(host, port, addresses):
    # The mock: dial the local origin whatever destination was screened.  The
    # proxy already proved it only calls this after a successful decision.
    return socket.create_connection(("127.0.0.1", origin_port), timeout=5)


server = build_server("127.0.0.1", 0, policy, guard=guard, connect=stub_connect)
proxy_port = server.server_address[1]
threading.Thread(target=server.serve_forever, daemon=True).start()

# Plain HTTP through the proxy (absolute-form request).
conn = http.client.HTTPConnection("127.0.0.1", proxy_port, timeout=5)
conn.request("GET", "http://example.com/hello")
resp = conn.getresponse()
body = resp.read()
check("an approved request is forwarded and answered", resp.status == 200 and body == b"hello from origin",
      f"status={resp.status} body={body!r}")

conn2 = http.client.HTTPConnection("127.0.0.1", proxy_port, timeout=5)
conn2.request("GET", "http://evil.example.net/steal")
resp2 = conn2.getresponse()
check("a refused request gets a 403 and no body from any origin",
      resp2.status == 403 and b"origin" not in resp2.read())

# CONNECT through the proxy, then a request inside the tunnel.
raw = socket.create_connection(("127.0.0.1", proxy_port), timeout=5)
raw.sendall(b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n")
head = raw.recv(4096)
check("an approved CONNECT establishes a tunnel", b"200" in head.split(b"\r\n")[0], head[:40])
raw.sendall(b"GET /tunnelled HTTP/1.1\r\nHost: example.com\r\nConnection: close\r\n\r\n")
tunnelled = b""
try:
    while True:
        chunk = raw.recv(4096)
        if not chunk:
            break
        tunnelled += chunk
except OSError:
    pass
raw.close()
check("bytes flow through the established tunnel", b"hello from origin" in tunnelled, tunnelled[:80])

# A refused CONNECT must not open a tunnel.
raw2 = socket.create_connection(("127.0.0.1", proxy_port), timeout=5)
raw2.sendall(b"CONNECT evil.example.net:443 HTTP/1.1\r\nHost: evil.example.net:443\r\n\r\n")
head2 = raw2.recv(4096)
raw2.close()
check("a refused CONNECT gets a 403, not a tunnel", b"403" in head2.split(b"\r\n")[0], head2[:40])

server.shutdown()
origin.shutdown()

print()
print("all restricted-gateway checks pass" if not failures else f"{len(failures)} gateway check(s) failed")
sys.exit(1 if failures else 0)
