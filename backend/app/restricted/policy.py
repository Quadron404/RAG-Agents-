"""The owner-controlled website allowlist for ordinary-agent browsing.

This module is the *policy*.  It answers one question -- may an ordinary agent
be permitted to reach this destination -- and it answers it the same way for the
browser, the fetcher, the search tool and the network gateway, so there is
exactly one place that decides and one set of rules to audit.

Default-deny is the whole design.  An empty or missing allowlist allows nothing,
and a destination that is not named is refused.  There is no wildcard-everything
form, no hidden exception and no environment variable that opens a hole: the
only way a destination becomes reachable is an operator editing the allowlist
file, and on the Codespace that file is ``/etc/rag-restricted/allowlist``, owned
by root with mode 0644, precisely so the unprivileged ordinary agent can read it
and cannot write it.  Nothing in this module writes the file, there is no setter
here, and no tool, prompt or conversation reaches one -- there is not one.

Layout: one rule per line.

    # comments and blank lines are ignored.
    example.com            # example.com, on 80 and 443, exact host only.
    *.example.com          # any subdomain of example.com; the apex is not one.
    api.example.com:8443   # one non-default port, that host.
    *.example.com:8443     # any subdomain, that port.

A bare ``*`` is refused as a rule, because "allow everything" typed once is the
failure mode default-deny exists to prevent.  A rule that carries a scheme, a
path, whitespace or a port outside 1..65535 is refused rather than half-read:
a malformed rule must not become a silent exception.  Hosts are lower-cased and
IDNA-encoded so a unicode look-alike cannot survive a byte comparison.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set
from urllib.parse import urlsplit

#: Ports a scheme-less or port-less rule permits.  A rule that names ``http`` or
#: ``https`` only in a URL still lands on one of these.
DEFAULT_PORTS: Set[int] = {80, 443}


@dataclass(frozen=True)
class Decision:
    """The verdict on one destination, with the reason kept for the audit log."""

    allowed: bool
    reason: str
    host: str = ""
    port: int = 0

    def __bool__(self) -> bool:  # so the gateway can branch on the decision
        return self.allowed


@dataclass(frozen=True)
class Rule:
    """One parsed allowlist line."""

    pattern: str
    wildcard: bool
    ports: Set[int]

    def matches(self, host: str) -> bool:
        if self.wildcard:
            # ``*.example.com`` matches ``a.example.com`` and ``a.b.example.com``
            # but not ``example.com`` itself: the apex is a different host and
            # an operator who wants both writes both.
            suffix = self.pattern[2:] if self.pattern.startswith("*.") else self.pattern
            return host.endswith("." + suffix) or host.endswith("." + suffix + ".")
        return host == self.pattern


def _normalise_host(raw: str) -> str:
    """Lower-case and IDNA-encode a host so look-alikes compare as bytes.

    ``urlsplit`` already strips brackets from an IPv6 literal; a trailing dot
    (a fully-qualified name) is removed so ``example.com.`` cannot be a second
    spelling of ``example.com``.
    """
    host = raw.strip().strip(".").lower()
    if not host:
        return ""
    try:
        # IDNA-encode the wire form; the ascii form is what DNS actually uses.
        return host.encode("idna").decode("ascii")
    except (UnicodeError, UnicodeDecodeError):
        return host


def _valid_host(host: str) -> bool:
    """True only for a plain ASCII DNS name.

    The IDNA codec is lenient about ASCII it does not understand -- a space
    survives it unchanged -- so the encoded form is checked here rather than
    trusted.  Letters, digits, hyphen and dot only; no empty label, no leading
    or trailing hyphen on a label.  This is what stops ``exa mple.com`` from
    being stored as a rule that can never match and a typo from being stored as
    one that can.
    """
    if not host or ".." in host or host.startswith(".") or host.endswith("."):
        return False
    for label in host.split("."):
        if not label or label[0] == "-" or label[-1] == "-":
            return False
        if not all(c.isascii() and (c.isalnum() or c == "-") for c in label):
            return False
    return True


def parse_rule(raw: str) -> Optional[Rule]:
    """Parse one line into a ``Rule``, or None when it is not a usable rule.

    Refusal, not repair: a line the parser does not fully understand is dropped
    and counted, so a typo narrows access instead of quietly widening it.
    """
    line = raw.strip()
    if not line or line.startswith("#"):
        return None

    # A URL pasted into the file is a mistake the operator should see: a rule is
    # a host, not an address.  Refusing it here keeps "https://" from ever being
    # read as a hostname that happens to contain a slash.
    if "://" in line or "/" in line:
        return None

    ports: Set[int] = set(DEFAULT_PORTS)
    host_part = line
    if line.count(":") == 1:
        # host:port.  A lone colon cannot appear in a DNS name, and an IPv6
        # literal carries more than one, so it is deliberately not split here;
        # IPv6 literals are not accepted as allowlist rules.
        host_part, _, port_text = line.partition(":")
        port_text = port_text.strip()
        if not port_text.isdigit():
            return None
        port = int(port_text)
        if not (1 <= port <= 65535):
            return None
        ports = {port}
    elif ":" in line:
        return None

    wildcard = host_part.startswith("*.")
    if not wildcard and "*" in host_part:
        # ``*`` alone, or ``a*b.com``.  Neither is a rule this parser can honour
        # exactly, and a suffix match built from a partial label is how a typo
        # turns into a hole.
        return None

    host = _normalise_host(host_part[2:] if wildcard else host_part)
    if not host or not _valid_host(host):
        return None
    # A wildcard over a bare TLD ("*.com") is refused: it names millions of
    # hosts an operator almost never means, and default-deny should not be one
    # keystroke away from open.
    if wildcard and "." not in host:
        return None

    pattern = ("*." + host) if wildcard else host
    return Rule(pattern=pattern, wildcard=wildcard, ports=frozenset(ports))


@dataclass
class Allowlist:
    """A parsed set of rules.  Immutable in use: the agent cannot add to it."""

    rules: List[Rule] = field(default_factory=list)
    rejected: List[str] = field(default_factory=list)

    @classmethod
    def from_text(cls, text: str) -> "Allowlist":
        rules: List[Rule] = []
        rejected: List[str] = []
        for raw in text.splitlines():
            stripped = raw.strip()
            if not stripped or stripped.startswith("#"):
                continue
            rule = parse_rule(stripped)
            if rule is None:
                rejected.append(stripped)
            else:
                rules.append(rule)
        return cls(rules=rules, rejected=rejected)

    def allows(self, host: str, port: int) -> Decision:
        host = _normalise_host(host)
        if not host:
            return Decision(False, "no host in destination", host, port)
        for rule in self.rules:
            if rule.matches(host) and port in rule.ports:
                return Decision(True, f"allowed by rule {rule.pattern}", host, port)
        # The reason a host was refused and the reason a port was refused are
        # different operator problems, so they are reported differently.
        for rule in self.rules:
            if rule.matches(host):
                return Decision(
                    False,
                    f"host {host} is allowed but not on port {port}",
                    host,
                    port,
                )
        return Decision(False, f"{host} is not in the allowlist", host, port)

    def __len__(self) -> int:
        return len(self.rules)

    def describe(self) -> str:
        return ", ".join(f"{r.pattern}:{','.join(str(p) for p in sorted(r.ports))}" for r in self.rules) or "(empty)"


class Policy:
    """The allowlist as the agent sees it: read-only, reloadable, fail-closed.

    A missing file is not an error to repair -- it is an empty allowlist, and an
    empty allowlist denies everything.  A malformed line is counted and dropped.
    Reload is driven by the file's modification time so an operator's edit takes
    effect without a restart while the agent can never trigger a write.
    """

    def __init__(self, path: str = "", *, text: Optional[str] = None):
        self.path = path
        self._text = text
        self._mtime: Optional[float] = None
        self._list = Allowlist()
        self.reload()

    @property
    def rules(self) -> Allowlist:
        return self._list

    def reload(self) -> Allowlist:
        if self._text is not None:
            self._list = Allowlist.from_text(self._text)
            return self._list
        import os

        if not self.path or not os.path.isfile(self.path):
            # No file, or a path that is not a file, means nothing is approved.
            self._mtime = None
            self._list = Allowlist()
            return self._list
        try:
            with open(self.path, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            self._mtime = os.path.getmtime(self.path)
        except OSError:
            self._list = Allowlist()
            return self._list
        self._list = Allowlist.from_text(text)
        return self._list

    def reload_if_changed(self) -> bool:
        if self._text is not None or not self.path:
            return False
        import os

        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            if self._mtime is not None:
                self._mtime = None
                self._list = Allowlist()
                return True
            return False
        if self._mtime is None or mtime != self._mtime:
            self.reload()
            return True
        return False

    def allows(self, host: str, port: int) -> Decision:
        self.reload_if_changed()
        return self._list.allows(host, port)

    def allows_url(self, url: str) -> Decision:
        """Scheme-aware convenience used by the tools: http(s) only, then host."""
        split = urlsplit(url.strip())
        if split.scheme.lower() not in ("http", "https"):
            return Decision(False, f"scheme {split.scheme or '(none)'} is not http or https")
        host = split.hostname or ""
        port = split.port or (443 if split.scheme.lower() == "https" else 80)
        return self.allows(host, port)

    def describe(self) -> Dict[str, object]:
        self.reload_if_changed()
        return {
            "path": self.path or "(built-in)",
            "rules": len(self._list),
            "rejected": list(self._list.rejected),
            "allow": self._list.describe(),
        }


#: Where the deployed allowlist lives.  Kept here as well as in config so the
#: gateway and the browser service, which may run as separate processes, agree
#: on the fallback order without importing the whole app.
DEPLOYED_ALLOWLIST = "/etc/rag-restricted/allowlist"


def default_allowlist_path() -> str:
    """The deployed path when it exists, else the repo fallback.

    The repo file exists so a developer can see the format and run the tests;
    it is not the boundary.  On the Codespace the root-owned ``/etc`` path is
    present and wins, which is what keeps the agent from editing its own rules.
    """
    import os

    if os.path.isfile(DEPLOYED_ALLOWLIST):
        return DEPLOYED_ALLOWLIST
    repo_fallback = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "restricted_allowlist.txt")
    return repo_fallback


def load_policy(settings) -> Policy:
    path = getattr(settings, "restricted_allowlist_path", "") or default_allowlist_path()
    return Policy(path)
