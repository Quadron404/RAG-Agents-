"""Destination screening: the second, independent check.

The allowlist decides *intent* -- which destinations an operator approved.  This
module decides *shape* -- whether a URL is a well-formed http(s) request to a
public destination at all, and where it actually points.  It exists because the
allowlist cannot be the only check:

* an approved hostname can resolve to a private address (DNS rebinding), so the
  name is allowed and the address is not;
* a URL can name a bare IP or an IPv6 literal and skip DNS entirely;
* a redirect can carry an approved request to a destination nobody approved;
* ``http://2130706433/`` and ``http://0x7f.1/`` are decimal and hex spellings of
  ``127.0.0.1`` that a naive string check reads as a hostname.

Each of those is refused here and refused again at the gateway, because a
browser-level URL check alone is not a boundary: the same address can be reached
by a shell, a second browser or a raw socket, and only the network layer sees
all of them.

Nothing here trusts the agent.  ``check`` takes a URL the agent wrote and
returns a verdict; the caller may ignore it, which is exactly why the gateway
performs the identical check on the wire.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple
from urllib.parse import urlsplit


class GuardError(ValueError):
    """A URL that cannot be screened at all (not http(s), no host, malformed)."""


#: Names that resolve inside the host or the cloud and must never be a browsing
#: destination, independent of what DNS says.  ``localhost`` and the metadata
#: name are the two an agent reaches for; the rest are their common aliases.
BLOCKED_HOSTNAMES = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "ip6-localhost",
        "ip6-loopback",
        "metadata",
        "metadata.google.internal",
        "metadata.goog",
        "instance-data",
    }
)

#: Cloud metadata addresses that are not inside a range ``ipaddress`` already
#: marks private, so they are named explicitly.  169.254.169.254 is in the
#: link-local range and would be caught anyway; the others pin the well-known
#: endpoints so a reader can see them and a test can assert them.
BLOCKED_ADDRESSES = frozenset(
    {
        "169.254.169.254",  # AWS/GCP/Azure instance metadata
        "169.254.170.2",    # ECS task metadata
        "100.100.100.200",  # Alibaba Cloud metadata
        "fd00:ec2::254",    # AWS IPv6 metadata
    }
)


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    reason: str
    host: str = ""
    port: int = 0
    addresses: Tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return self.allowed


def _is_blocked_address(ip: str) -> Optional[str]:
    """Why an address is not a public internet destination, or None if it is.

    Every non-global category is refused, not just 127.0.0.1: private, loopback,
    link-local, reserved, unspecified, multicast and the CGNAT range all name
    somewhere that is not the public web, and an agent has no business there.
    """
    if ip in BLOCKED_ADDRESSES:
        return f"{ip} is a cloud metadata address"
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return f"{ip} is not an IP address"
    if addr.is_loopback:
        return f"{ip} is a loopback address"
    if addr.is_link_local:
        return f"{ip} is a link-local address"
    if addr.is_private:
        return f"{ip} is a private address"
    if addr.is_reserved:
        return f"{ip} is a reserved address"
    if addr.is_multicast:
        return f"{ip} is a multicast address"
    if addr.is_unspecified:
        return f"{ip} is the unspecified address"
    # CGNAT (100.64.0.0/10) is not "private" to ipaddress but is carrier-internal
    # and not a public destination.
    if isinstance(addr, ipaddress.IPv4Address) and addr in ipaddress.ip_network("100.64.0.0/10"):
        return f"{ip} is a carrier-grade NAT address"
    return None


def _default_resolve(host: str) -> List[str]:
    """Resolve a host to every address DNS returns.

    ``getaddrinfo`` is used rather than ``gethostbyname`` so IPv6 answers count
    too: a host that is public over IPv4 and loopback over IPv6 must be refused,
    and the old call would have seen only the v4 half.
    """
    infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    addresses = []
    for info in infos:
        sockaddr = info[4]
        addr = sockaddr[0]
        if addr not in addresses:
            addresses.append(addr)
    return addresses


class Guard:
    """Screens URLs against scheme, host, port, resolved address and redirect.

    ``resolve`` is injectable so the policy of *which addresses are blocked* can
    be tested for real without a DNS server, and so a deployment can swap in a
    resolver that pins DNS.  The screening itself is never mocked: tests pass
    real IP literals and a real resolver function.
    """

    def __init__(self, resolve: Optional[Callable[[str], List[str]]] = None):
        self._resolve = resolve or _default_resolve

    def parse(self, url: str) -> Tuple[str, str, int]:
        """Return (scheme, host, port) or raise ``GuardError``."""
        raw = (url or "").strip()
        if not raw:
            raise GuardError("empty URL")
        split = urlsplit(raw)
        scheme = split.scheme.lower()
        if scheme not in ("http", "https"):
            raise GuardError(f"scheme {scheme or '(none)'} is not http or https")
        if split.username or split.password:
            # userinfo is a classic way to make a URL read as one host while
            # connecting to another, and no approved destination needs it.
            raise GuardError("URLs with credentials in the authority are refused")
        host = split.hostname or ""
        if not host:
            raise GuardError("URL has no host")
        try:
            port = split.port
        except ValueError:
            raise GuardError("URL has an invalid port")
        if port is None:
            port = 443 if scheme == "https" else 80
        if not (1 <= port <= 65535):
            raise GuardError(f"port {port} is out of range")
        return scheme, host, port

    def check(self, url: str) -> Verdict:
        """Screen one absolute http(s) URL.  Never raises for a bad destination."""
        try:
            scheme, host, port = self.parse(url)
        except GuardError as exc:
            return Verdict(False, str(exc))

        bare = host.strip("[]").lower()
        if bare in BLOCKED_HOSTNAMES:
            return Verdict(False, f"{host} is a blocked host name", host, port)

        # A host that is itself an IP literal must be screened directly; there is
        # no DNS to consult and no name to trust.
        try:
            ipaddress.ip_address(bare)
            literal = True
        except ValueError:
            literal = False

        if literal:
            why = _is_blocked_address(bare)
            if why:
                return Verdict(False, why, host, port)
            return Verdict(True, "public IP literal", host, port, (bare,))

        # A name that looks like an integer or hex is a numeric spelling of an
        # address (``2130706433`` == 127.0.0.1); resolve it and screen the
        # result rather than trusting the label.
        try:
            addresses = self._resolve(host)
        except (socket.gaierror, UnicodeError, OSError) as exc:
            return Verdict(False, f"{host} did not resolve: {exc}", host, port)
        if not addresses:
            return Verdict(False, f"{host} did not resolve", host, port)

        for addr in addresses:
            why = _is_blocked_address(addr)
            if why:
                # Refuse if *any* answer is non-public: a name with one public
                # and one private answer is a rebinding attempt, and picking the
                # public one would be exactly the wrong bet.
                return Verdict(False, f"{host} resolves to {why}", host, port, tuple(addresses))
        return Verdict(True, "public destination", host, port, tuple(addresses))

    def check_redirect(self, from_url: str, to_url: str) -> Verdict:
        """A redirect is a new destination and is screened from scratch.

        The origin is accepted as already-screened so the error can name it;
        the target gets the same full check a first navigation gets.
        """
        verdict = self.check(to_url)
        return verdict
