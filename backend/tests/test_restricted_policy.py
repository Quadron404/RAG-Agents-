"""The owner-controlled allowlist: default-deny, exact, and not writable.

Ordinary agents may only reach destinations the owner named.  These checks pin
the two halves of that promise: a named destination is allowed on the ports the
rule names and nothing else, and an UNnamed one -- including everything a typo,
a wildcard or a missing file could accidentally open -- is refused.  The last
block proves the agent cannot edit the policy through this object at all, because
the absence of a setter is the guarantee, not a comment saying there is none.
"""
import os
import sys
import tempfile

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND_DIR)

from app.restricted.policy import Allowlist, Policy, parse_rule

failures = []


def check(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"ok   {label}")
    else:
        failures.append(label)
        print(f"FAIL {label} {detail}")


# --- default deny ---------------------------------------------------------
empty = Allowlist.from_text("")
check("an empty allowlist allows nothing", empty.allows("example.com", 443).allowed is False)
check("an empty allowlist denies a bare host", empty.allows("", 80).allowed is False)

# --- a plain host rule ----------------------------------------------------
sample = Allowlist.from_text(
    "# a comment, ignored\n"
    "\n"
    "example.com\n"
    "api.example.com:8443\n"
    "*.docs.example.com\n"
)
check("an exact host is allowed on https", sample.allows("example.com", 443).allowed is True)
check("an exact host is allowed on http", sample.allows("example.com", 80).allowed is True)
check("an exact host is refused on a port the rule did not name", sample.allows("example.com", 8080).allowed is False)
check("a subdomain is not the apex", sample.allows("www.example.com", 443).allowed is False)
check("an explicit host:port is allowed on that port", sample.allows("api.example.com", 8443).allowed is True)
check("an explicit host:port is refused on the default port", sample.allows("api.example.com", 443).allowed is False)
check("a wildcard matches a subdomain", sample.allows("a.docs.example.com", 443).allowed is True)
check("a wildcard matches a deeper subdomain", sample.allows("a.b.docs.example.com", 443).allowed is True)
check("a wildcard does not match the wildcard's own apex", sample.allows("docs.example.com", 443).allowed is False)
check("an unlisted host is refused", sample.allows("evil.com", 443).allowed is False)

# --- what must never become a rule ---------------------------------------
check("a bare * is refused as a rule", parse_rule("*") is None)
check("a partial-label wildcard is refused", parse_rule("a*b.example.com") is None)
check("a wildcard over a bare TLD is refused", parse_rule("*.com") is None)
check("a rule with a scheme is refused", parse_rule("https://example.com") is None)
check("a rule with a path is refused", parse_rule("example.com/foo") is None)
check("a rule with whitespace inside is refused", parse_rule("exa mple.com") is None)
check("a rule with a bad port is refused", parse_rule("example.com:99999") is None)
check("a rule with a non-numeric port is refused", parse_rule("example.com:https") is None)
check("a rule with an IPv6 literal is refused", parse_rule("[::1]:80") is None)

# --- normalisation --------------------------------------------------------
upper = Allowlist.from_text("Example.COM")
check("host matching is case-insensitive", upper.allows("EXAMPLE.com", 443).allowed is True)
check("a trailing dot is not a second spelling", upper.allows("example.com.", 443).allowed is True)

# --- the file on disk and reload -----------------------------------------
tmp = tempfile.mkdtemp(prefix="rag-restricted-policy-")
path = os.path.join(tmp, "allowlist")
with open(path, "w", encoding="utf-8") as fh:
    fh.write("one.example\n")
policy = Policy(path)
check("a policy reads the file it names", policy.allows("one.example", 443).allowed is True)
check("a host not in the file is denied", policy.allows("two.example", 443).allowed is False)

with open(path, "w", encoding="utf-8") as fh:
    fh.write("two.example\n")
check("reload_if_changed notices an edit", policy.reload_if_changed() is True)
check("the edited rule takes effect without a restart", policy.allows("two.example", 443).allowed is True)
check("the removed rule is gone", policy.allows("one.example", 443).allowed is False)

os.remove(path)
check("deleting the file denies everything again", policy.allows("two.example", 443).allowed is False)

missing = Policy(os.path.join(tmp, "does-not-exist"))
check("a missing file is an empty allowlist, not an error", missing.allows("example.com", 443).allowed is False)

# --- the agent cannot edit the policy ------------------------------------
# The guarantee is structural: this object exposes no method that adds, removes
# or overrides a rule, so no tool call, prompt or conversation can reach one.
for forbidden in ("add", "allow", "append", "insert", "set", "update", "merge", "extend", "write", "save", "grant"):
    check(f"Policy has no {forbidden}() to widen itself", not hasattr(Policy, forbidden))
check("Allowlist exposes no mutator either", not any(hasattr(Allowlist, m) for m in ("add", "append", "insert", "extend")))
check("parse_rule returns a frozen rule", isinstance(parse_rule("example.com").ports, frozenset))

print()
print("all restricted-policy checks pass" if not failures else f"{len(failures)} policy check(s) failed")
sys.exit(1 if failures else 0)
