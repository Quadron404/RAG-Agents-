"""The tunnel script has to notice the URL cloudflared hands it.

The Quick Tunnel does not take a hostname as input -- it is assigned one, and
the only way to learn it is to read cloudflared's log.  So the extraction is the
one piece of this feature that has to work against output nobody here controls,
and it is also the piece that decides whether the app advertises a live URL or a
stale one after a restart.

These cases run the script's own scan() over realistic cloudflared output.  The
script is sourced rather than executed, so no tunnel is started and cloudflared
does not need to be installed.

Run with:  python -m tests.test_tunnel_url
"""
import pathlib
import shutil
import subprocess
import sys
import tempfile

CODES = pathlib.Path(__file__).resolve().parents[2] / "codespace"
SCRIPT = CODES / "start-tunnel.sh"


def find_bash() -> str | None:
    """Locate bash, including Git Bash on Windows where it is not on PATH.

    Deployed this runs on Linux, but the same tests have to pass on a developer
    machine, and a suite that silently skips itself is a suite that stops
    catching things.
    """
    found = shutil.which("bash")
    if found:
        return found
    candidates = [
        "/bin/bash",
        "/usr/bin/bash",
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files\Git\usr\bin\bash.exe",
        r"C:\Windows\System32\bash.exe",
    ]
    for path in candidates:
        if pathlib.Path(path).exists():
            return path
    return None


BASH = find_bash()

failures: list[str] = []


def check(cond: bool, label: str) -> None:
    print(f"{'ok  ' if cond else 'FAIL'}  {label}")
    if not cond:
        failures.append(label)


# What cloudflared actually prints for a quick tunnel: a banner, the URL inside a
# box of rules, and a stream of status lines after it.
BOXED = """\
2026-02-11T09:14:02Z INF Thank you for trying Cloudflare Tunnel...
2026-02-11T09:14:02Z INF +--------------------------------------------------------------------------------------------+
2026-02-11T09:14:02Z INF |  Your quick Tunnel has been created! Visit it at (it may take some time to be reachable):  |
2026-02-11T09:14:02Z INF |  https://witty-pandas-repeat-7x9k.trycloudflare.com                                                  |
2026-02-11T09:14:02Z INF +--------------------------------------------------------------------------------------------+
2026-02-11T09:14:05Z INF Starting tunnel...
2026-02-11T09:14:06Z INF Connection registered connIndex=0
"""

# A reconnect after a dropped edge prints the URL again, sometimes bare.
RECONNECTED = """\
2026-02-11T10:02:44Z INF Connection registered connIndex=0
2026-02-11T10:05:00Z INF https://fresh-mango-kettle-2b4x.trycloudflare.com
2026-02-11T10:05:01Z INF Connection registered connIndex=1
"""

NOISY = """\
2026-02-11T09:14:02Z INF Cannot determine default configuration path.
2026-02-11T09:14:02Z INF No ingress rules in the config file.
2026-02-11T09:14:03Z INF Starting metrics server on 127.0.0.1:12345
"""


def run_scan(stdin_text: str, workdir: pathlib.Path, name: str = "public-url") -> tuple[int, str, str]:
    """Source the script and pipe output through its scan().

    The path is passed as a bare filename with the cwd set, because a Windows
    path is not a valid POSIX filename: bash would treat "C:\\...\\public-url"
    as one literal name in the current directory and quietly write the URL
    somewhere this test never looks.
    """
    script = f"""
set -uo pipefail
export PUBLIC_URL_FILE="{name}"
source "{SCRIPT}" >/dev/null 2>&1
scan
"""
    run = subprocess.run(
        [BASH, "-c", script],
        input=stdin_text,
        capture_output=True,
        text=True,
        cwd=str(workdir),
    )
    return run.returncode, run.stdout.strip(), run.stderr.strip()


if BASH is None:
    print("SKIP: no bash available, so the tunnel script cannot be exercised")
    raise SystemExit(0)

check(SCRIPT.exists(), "start-tunnel.sh is present")

with tempfile.TemporaryDirectory() as tmp:
    work = pathlib.Path(tmp)
    url_file = work / "public-url"

    # --- the normal case ---------------------------------------------------
    rc, out, err = run_scan(BOXED, work)
    check(rc == 0, f"scan exits cleanly on real cloudflared output (rc={rc} {err[:80]})")
    got = url_file.read_text() if url_file.exists() else ""
    check(
        got == "https://witty-pandas-repeat-7x9k.trycloudflare.com",
        f"the boxed banner URL is extracted and written (got {got!r})",
    )
    check("https://" not in out, "the URL is not echoed to the log stream a second time")

    # --- a restart must overwrite, not accumulate --------------------------
    rc, _out, _err = run_scan(RECONNECTED, work)
    got = url_file.read_text() if url_file.exists() else ""
    check(
        got == "https://fresh-mango-kettle-2b4x.trycloudflare.com",
        f"a reconnect replaces the previous URL (got {got!r})",
    )
    check(
        "witty-pandas-repeat" not in got,
        "the old hostname does not survive a restart, which is the whole point",
    )

    # --- output with no URL leaves no file ---------------------------------
    url_file.unlink(missing_ok=True)
    rc, _out, _err = run_scan(NOISY, work)
    check(rc == 0, "scan exits cleanly when there is no URL in the output")
    check(not url_file.exists(), "no URL file is invented from unrelated log lines")

    # --- the last one wins -------------------------------------------------
    url_file.unlink(missing_ok=True)
    rc, _out, _err = run_scan(BOXED + RECONNECTED, work)
    got = url_file.read_text() if url_file.exists() else ""
    check(
        got == "https://fresh-mango-kettle-2b4x.trycloudflare.com",
        f"with two URLs the most recent is the one published (got {got!r})",
    )

    # --- and the script does not start a tunnel when merely sourced --------
    rc, _out, _err = run_scan("", work)
    check(rc == 0, "sourcing the script for its functions does not run cloudflared")

print()
if failures:
    print(f"FAIL: {len(failures)} check(s) did not hold")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("PASS: the tunnel script finds the URL cloudflared prints, and follows a restart")
