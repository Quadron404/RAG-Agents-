"""Mutate each listener to a wildcard bind, prove the suite catches it, restore.

A check that has never been seen to fail is not evidence of anything.  The
static suite passed while the live Codespace run reported three failures, so
these mutations are how we find out whether the new assertions actually bite --
and they immediately paid for themselves by exposing that the `-localhost`
assertion was satisfied by a *comment* rather than by the flag.

Mutations are applied with a regex anchored to the start of a code line, so a
mutation can never land in a comment.  Replacing the first textual occurrence
would silently test nothing at all.

Run with:  python -m tests.test_bind_mutations
"""
import pathlib
import re
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]
BSLASH = chr(92)  # a single backslash, without fighting the string escaping below

# (file, regex anchored to a code line, replacement, what it represents)
#
# Every pattern ends in \r?$ rather than $: half the tree is checked out with
# CRLF on Windows, and a bare $ would not match before the \r, so the mutation
# would silently match nothing.
MUTATIONS = (
    (
        "backend/app/config.py",
        r'^(\s*).*_get\("HOST", "127\.0\.0\.1"\).*\r?$',
        r'\1host: str = field(default_factory=lambda: _get("HOST", "0.0.0.0"))',
        "the app defaulting to every interface",
    ),
    (
        "backend/vm_agent/daemon.py",
        r'^(\s*)def main\(port: int = 9000, host: str = "127\.0\.0\.1"\):\r?$',
        r'\1def main(port: int = 9000, host: str = "0.0.0.0"):',
        "the agent daemon defaulting to every interface",
    ),
    (
        "backend/vm_agent/daemon.py",
        r'^(\s*)server = ThreadingHTTPServer\(\(host, port\), Handler\)\r?$',
        r'\1server = ThreadingHTTPServer(("0.0.0.0", port), Handler)',
        "the agent server ignoring its loopback host",
    ),
    (
        "codespace/boot.sh",
        r'^(\s*)exec "\$BACKEND_PY" -m uvicorn app\.main:app --host 127\.0\.0\.1 (.*)\r?$',
        r'\1exec "$BACKEND_PY" -m uvicorn app.main:app --host 0.0.0.0 \2',
        "uvicorn launched on every interface",
    ),
    (
        "codespace/start-computer.sh",
        # \\ in the regex source = a literal backslash, then end of line.
        r"^(\s*)-localhost " + BSLASH * 2 + r"\r?$",
        r"\1-listen 0.0.0.0 " + BSLASH,
        "x11vnc answering on every interface (start-computer.sh)",
    ),
    (
        "backend/vm_agent/daemon.py",
        r'^(\s*)"-localhost",.*\r?$',
        r'\1"-listen", "0.0.0.0",',
        "x11vnc answering on every interface (daemon.py)",
    ),
    (
        "codespace/start-computer.sh",
        r"^(\s*)-ac -nolisten tcp(.*)\r?$",
        r"\1-ac " + BSLASH,
        "Xvfb accepting TCP clients (start-computer.sh)",
    ),
    (
        "backend/.env.example",
        r"^HOST=127\.0\.0\.1\r?$",
        "HOST=0.0.0.0",
        "the committed .env template publishing the app",
    ),
)


def run_isolation() -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "tests.test_screen_isolation"],
        capture_output=True,
        text=True,
        cwd=REPO / "backend",
    )
    return proc.returncode, (proc.stdout or proc.stderr).strip()


def main() -> int:
    failures: list[str] = []

    baseline_rc, _ = run_isolation()
    if baseline_rc != 0:
        print("FAIL  the suite does not pass on the real tree, so mutations prove nothing")
        return 1
    print("ok    the suite passes on the real tree")

    for rel, pattern, repl, label in MUTATIONS:
        path = REPO / rel
        # Read and write with newline="" so the bytes round-trip exactly.  With
        # the default universal newlines, writing the file back on Windows would
        # convert every LF to CRLF, and a test that silently rewrites line
        # endings of shell scripts is a bug factory.
        with path.open("r", encoding="utf-8", newline="") as fh:
            original = fh.read()

        # Resolve \1-style references by hand rather than through re's template
        # parser: a replacement containing a literal backslash (the shell line
        # continuation) is a "bad escape" to that parser, and a callable lets the
        # backslash through untouched.
        def _repl(m: "re.Match[str]", _r=repl) -> str:
            return re.sub(r"\\(\d+)", lambda g: m.group(int(g.group(1))), _r)

        mutated, count = re.subn(pattern, _repl, original, flags=re.MULTILINE)
        if count == 0:
            failures.append(f"{rel}: mutation for {label!r} matched nothing")
            print(f"FAIL  {label}: no line matched, so nothing was tested")
            continue
        if mutated == original:
            failures.append(f"{rel}: mutation for {label!r} was a no-op")
            print(f"FAIL  {label}: the mutation changed nothing")
            continue

        with path.open("w", encoding="utf-8", newline="") as fh:
            fh.write(mutated)
        try:
            rc, out = run_isolation()
        finally:
            with path.open("w", encoding="utf-8", newline="") as fh:
                fh.write(original)

        if rc == 0:
            failures.append(f"{rel}: {label} is NOT caught by the suite")
            print(f"FAIL  {label} (mutation went undetected)")
        elif "SyntaxError" in out or "Traceback" in out:
            failures.append(f"{rel}: {label} produced a crash, not a real failure")
            print(f"FAIL  {label} (crashed instead of failing the check)")
        else:
            first = next(
                (
                    ln.strip().lstrip("- ").strip()
                    for ln in out.splitlines()
                    if any(
                        k in ln
                        for k in ("0.0.0.0", "127.0.0.1", "localhost", "interface", "loopback")
                    )
                ),
                "",
            )
            print(f"ok    {label}")
            if first:
                print(f"        caught by: {first[:104]}")

    # The tree must be byte-identical afterwards, or a mutation leaked.
    print()
    if failures:
        print(f"FAIL: {len(failures)} mutation(s) were not caught")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS: every listener mutation is caught, and the tree was restored")
    return 0


if __name__ == "__main__":
    sys.exit(main())
