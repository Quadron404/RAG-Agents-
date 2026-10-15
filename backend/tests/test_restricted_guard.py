"""Destination screening: scheme, host, port, DNS result and redirect.

These are the checks the allowlist cannot make on its own.  A hostname can
resolve to the private network, a URL can name a bare IP, and a decimal or hex
spelling of an address slips past a string comparison -- each is refused here
and refused again on the wire at the gateway.  The resolver is injected so the
rules about *what counts as a blocked address* are exercised for real, with real
``ipaddress`` logic, without depending on a DNS server being up.
"""
import os
import sys

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND_DIR)

from app.restricted.guard import Guard, GuardError, _is_blocked_address

failures = []


def check(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"ok   {label}")
    else:
        failures.append(label)
        print(f"FAIL {label} {detail}")


# A resolver that answers like a well-behaved DNS server would.
TABLE = {
    "example.com": ["93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"],
    "docs.example.com": ["93.184.216.35"],
    "rebind.example": ["93.184.216.34", "127.0.0.1"],
    "internal.example": ["10.1.2.3"],
    "2130706433": ["127.0.0.1"],
    "169.254.169.254.nip.io": ["169.254.169.254"],
    "metadata.google.internal": ["169.254.169.254"],
    "evil.example.net": ["93.184.216.20"],
    "nothing.example": [],
}


def resolve(host: str):
    if host not in TABLE:
        raise OSError(f"no such host: {host}")
    return TABLE[host]


guard = Guard(resolve=resolve)

# --- scheme and shape -----------------------------------------------------
check("a plain https URL is well formed", guard.check("https://example.com/x").allowed is True)
check("http is allowed too", guard.check("http://example.com/").allowed is True)
check("ftp is refused", guard.check("ftp://example.com/f").allowed is False)
check("a data: URL is refused", guard.check("data:text/html,hi").allowed is False)
check("a file: URL is refused", guard.check("file:///etc/passwd").allowed is False)
check("a scheme-relative //host is refused", guard.check("//example.com").allowed is False)
check("an empty URL is refused", guard.check("").allowed is False)
check("credentials in the authority are refused", guard.check("http://user:pass@example.com/").allowed is False)
check("userinfo cannot dress up a private host", guard.check("http://example.com@127.0.0.1/").allowed is False)

# --- host names -----------------------------------------------------------
check("localhost is refused", guard.check("http://localhost:8000/").allowed is False)
check("localhost.localdomain is refused", guard.check("http://localhost.localdomain/").allowed is False)
check("the metadata name is refused", guard.check("http://metadata.google.internal/").allowed is False)
check("a public hostname is allowed", guard.check("https://docs.example.com/").allowed is True)
check("an unresolvable host is refused", guard.check("https://nothing.example/").allowed is False)

# --- bare address literals ------------------------------------------------
check("a loopback literal is refused", guard.check("http://127.0.0.1/").allowed is False)
check("another loopback literal is refused", guard.check("http://127.9.9.9/").allowed is False)
check("an IPv6 loopback literal is refused", guard.check("http://[::1]/").allowed is False)
check("a private IPv4 literal is refused", guard.check("http://10.0.0.1/").allowed is False)
check("a 192.168 class literal is refused", guard.check("http://192.168.1.1/").allowed is False)
check("a 172.16 class literal is refused", guard.check("http://172.16.5.4/").allowed is False)
check("a link-local literal is refused", guard.check("http://169.254.1.1/").allowed is False)
check("the cloud metadata literal is refused", guard.check("http://169.254.169.254/").allowed is False)
check("the GCP metadata literal is refused", guard.check("http://metadata.goog/").allowed is False)
check("a CGNAT literal is refused", guard.check("http://100.64.0.1/").allowed is False)
check("a public IPv4 literal is allowed", guard.check("http://93.184.216.34/").allowed is True)
check("an unspecified address is refused", guard.check("http://0.0.0.0/").allowed is False)

# --- spellings of an address ---------------------------------------------
check("a decimal spelling of loopback is refused", guard.check("http://2130706433/").allowed is False)
check("a dotted-decimal-looking name is resolved and screened",
      guard.check("http://169.254.169.254.nip.io/").allowed is False)

# --- DNS rebinding --------------------------------------------------------
check("a name that resolves to a private address is refused",
      guard.check("https://internal.example/").allowed is False)
check("a name with one public and one private answer is refused",
      guard.check("https://rebind.example/").allowed is False)
verdict = guard.check("https://rebind.example/")
check("the refusal names the rebinding address", "127.0.0.1" in verdict.reason, verdict.reason)

# --- redirect -------------------------------------------------------------
check("a redirect to an approved host is allowed",
      guard.check_redirect("https://example.com/", "https://docs.example.com/").allowed is True)
check("a redirect to a private address is refused",
      guard.check_redirect("https://example.com/", "http://127.0.0.1/").allowed is False)
check("a redirect to a public host passes the shape check (the allowlist refuses it next)",
      guard.check_redirect("https://example.com/", "https://evil.example.net/").allowed is True)

# --- the address rules themselves ----------------------------------------
check("loopback is blocked", _is_blocked_address("127.0.0.1") is not None)
check("RFC1918 is blocked", _is_blocked_address("10.0.0.1") is not None)
check("link-local is blocked", _is_blocked_address("169.254.0.1") is not None)
check("metadata is blocked by name", _is_blocked_address("169.254.169.254") is not None)
check("a public address is not blocked", _is_blocked_address("93.184.216.34") is None)
check("an IPv6 ULA is blocked", _is_blocked_address("fc00::1") is not None)

# --- parsing helpers ------------------------------------------------------
try:
    guard.parse("http://example.com:99999/")
    check("an out-of-range port raises", False)
except GuardError:
    check("an out-of-range port raises", True)

print()
print("all restricted-guard checks pass" if not failures else f"{len(failures)} guard check(s) failed")
sys.exit(1 if failures else 0)
