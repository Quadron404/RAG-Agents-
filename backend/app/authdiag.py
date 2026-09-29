"""Say which passphrase the server is actually using, without printing it.

Run from the Codespace:

    cd backend && .venv/bin/python -m app.authdiag
    cd backend && .venv/bin/python -m app.authdiag --check 'the-passphrase-i-typed'

Why this exists.  The login screen reports every failure the same way, whether
you typed the wrong passphrase or the server never loaded one at all, or loaded
a different one than the line you are looking at in backend/.env.  Those need
three different fixes and the error message cannot tell them apart.  This prints
the three facts that do: is a value loaded, where it came from, and a short
SHA-256 fingerprint of it.

The fingerprint is a hash, not an encoding.  It does not reveal the passphrase,
it does not decrypt anything, and comparing fingerprints answers the only
question that matters here -- "is what I typed what the server loaded?" -- without
either value being written to a terminal, a log, or a shell history.
"""

from __future__ import annotations

import argparse
import json
import sys

from . import auth
from . import config


def _candidate_fingerprint(candidate: str) -> str:
    return config._fingerprint(candidate.strip())


def _render(info: dict, candidate: str | None = None) -> str:
    lines = []
    lines.append("RAG_AUTH_TOKEN")
    lines.append(f"  loaded       : {'yes' if info.get('loaded') else 'NO'}")
    lines.append(f"  source       : {info.get('source', 'unset')}")
    lines.append(f"  fingerprint  : {info.get('fingerprint') or '(none)'}  (sha256, first 12)")
    if info.get("duplicate_lines_in_file"):
        lines.append(
            f"  duplicates   : {info['duplicate_lines_in_file']} later "
            "RAG_AUTH_TOKEN line(s) in backend/.env, ignored -- the first one wins"
        )
    if info.get("shadowed_file_value"):
        lines.append("  shadowed     : the process environment sets this key, so backend/.env is ignored for it")
        if info.get("note"):
            lines.append(f"                {info['note']}")
    if not info.get("loaded"):
        lines.append("")
        lines.append("  Nothing is configured, so every route except /health and /auth/*")
        lines.append("  is refused.  Set it with:")
        lines.append("    printf 'RAG_AUTH_TOKEN=%s\\n' \"$T\" >> backend/.env")
        lines.append("    bash codespace/boot.sh")
    if candidate is not None:
        got = _candidate_fingerprint(candidate)
        want = info.get("fingerprint") or ""
        lines.append("")
        if not want:
            lines.append("  No passphrase is loaded, so nothing can match.")
        elif got == want:
            lines.append("  That passphrase MATCHES the one the server loaded.")
            lines.append("  If the login screen still rejects it, the browser is holding a")
            lines.append("  stale session or the request is going somewhere else -- check the")
            lines.append("  URL in the address bar against the tunnel hostname.")
        else:
            lines.append(f"  That passphrase does NOT match.  typed={got}  server={want}")
            if info.get("source") == "backend/.env":
                lines.append("  The server is using backend/.env.  Compare against the FIRST")
                lines.append("  RAG_AUTH_TOKEN line in that file, not the last.")
            if info.get("shadowed_file_value"):
                lines.append("  The server is using the process environment, not backend/.env.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="app.authdiag", description=__doc__)
    ap.add_argument(
        "--check",
        metavar="PASS",
        help="compare a candidate passphrase against the loaded one by fingerprint",
    )
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)

    info = auth.describe()
    if args.json:
        out = dict(info)
        if args.check is not None:
            out["candidate_fingerprint"] = _candidate_fingerprint(args.check)
            out["candidate_matches"] = bool(out.get("fingerprint")) and out[
                "candidate_fingerprint"
            ] == out["fingerprint"]
        print(json.dumps(out, indent=2))
        return 0 if info.get("loaded") else 1

    print(_render(info, args.check))
    return 0 if info.get("loaded") else 1


if __name__ == "__main__":
    sys.exit(main())
