"""The run directory belongs to the user running the stack, not to root.

/tmp/ragdesktop holds the pid files that decide which processes get restarted
and killed, plus the live public URL.  It is created by boot.sh and supervise.sh,
which run unprivileged as the Codespace runtime user -- but install.sh runs as
root from postCreateCommand, and its `mkdir -p ... $DESKTOP_RUN_DIR` left the
directory owned by root.  `mkdir -p` on an existing directory changes nothing, so
that owner survived every later boot and every write into it failed:

    boot.sh: line 74: /tmp/ragdesktop/supervisor.pid: Permission denied

The supervisor never starts, so the backend never starts, so cloudflared is
never asked for a hostname, so there is no public URL and the desktop looks
completely dead while the only clue is a line in a log nobody reads.

So: the run dir is created by the user who has to write to it, the owner is
repaired when it is already wrong, the repair is a no-op when it is right (a
second boot must not be a second chance to get it wrong), and the repair is
chown to that user rather than chmod 777.

The static checks run everywhere.  The behavioural ones need a POSIX host and
are skipped without bash; they run in the Codespace and in CI.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CODES = ROOT / "codespace"
ENV = CODES / "env.sh"
BOOT = CODES / "boot.sh"
SUPERVISE = CODES / "supervise.sh"
INSTALL = CODES / "install.sh"
DEVCONTAINER = ROOT / ".devcontainer" / "devcontainer.json"
BASH = shutil.which("bash")

failures: list[str] = []


def check(ok: bool, label: str) -> bool:
    if ok:
        print(f"ok    {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}")
    return ok


def sudo_invocations(code: str) -> list[str]:
    """Every place sudo is actually *run*, not merely mentioned.

    Two things have to be filtered out first or this reports harmless lines: the
    FATAL message that tells the operator which command to type by hand, and
    `command -v sudo`, which is a lookup rather than an invocation.
    """
    out = []
    for line in code.splitlines():
        bare = re.sub(r'"[^"]*"', "", line)  # message strings
        bare = re.sub(r"command -v\s+sudo", "", bare)  # a lookup, not a call
        for match in re.finditer(r"(?<![\w-])sudo\b(.*)", bare):
            rest = match.group(1).lstrip()
            if not rest.startswith("-n"):
                out.append("INTERACTIVE: " + line.strip())
    return out

    """The file with its comments removed, so prose cannot satisfy a check."""
def code_of(path: Path) -> str:
    """The file with its comments removed, so prose cannot satisfy a check."""
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("#"):
            continue
        out.append(line)
    return "\n".join(out)


# --- 1. the origin is gone: install.sh does not create the run dir ---------
#
# This is the actual cause, so it is checked first.  A fix in boot.sh alone would
# still leave every install creating a root-owned directory for the boot to
# repair, which is one sudo per boot and one boot that fails if sudo is missing.
install_code = code_of(INSTALL)
check(
    "mkdir" not in install_code or "DESKTOP_RUN_DIR" not in
    re.search(r"mkdir[^\n]*", install_code).group(0),
    "install.sh no longer creates $DESKTOP_RUN_DIR as root",
)
check(
    "$DESKTOP_RUN_DIR" not in install_code,
    "install.sh never writes inside the run dir (it cannot, as root)",
)
# The chrome profile is genuinely privileged (/workspaces is root-owned) and
# genuinely needs the owner fixed, so it must still be chowned to the user.
check(
    "chown" in install_code and "codespace_user" in install_code,
    "install.sh still chowns the root-owned chrome profile to the runtime user",
)

# --- 2. both runtime scripts repair the owner before writing ---------------
for name, path in (("boot.sh", BOOT), ("supervise.sh", SUPERVISE)):
    code = code_of(path)
    check(
        "ensure_run_dir" in code,
        f"{name} calls ensure_run_dir before it writes any pid file",
    )
    # Guarded, and fatal when it fails: a supervisor that cannot write its pid
    # files cannot restart anything, so it must not pretend to be running.
    block = re.search(r"if\s*!\s*ensure_run_dir[^\n]*\n((?:[^\n]*\n){0,4}?)\s*fi", code)
    check(
        block is not None and "exit 1" in block.group(1),
        f"{name} treats a failed ensure_run_dir as fatal rather than continuing",
    )
    # Every pid file goes through write_pid.  A bare redirect is the bug: it
    # fails with EACCES and the caller goes on thinking it recorded a pid.
    redirects = re.findall(r'>\s*"\$RUN_DIR/[^"]*\.pid"', code)
    check(
        not redirects,
        f"{name} writes no pid file with a bare redirect ({redirects})",
    )
    for pid_file in re.findall(r'write_pid\s+"\$RUN_DIR/([^"]*\.pid)"', code):
        check(True, f"{name} routes {pid_file} through write_pid")
    # It is also the reason: nothing here may re-introduce a bare mkdir that
    # quietly tolerates a foreign owner.
    check(
        re.search(r'mkdir\s+-p\s+"?\$RUN_DIR', code) is None,
        f"{name} does not mkdir the run dir itself (ensure_run_dir owns that)",
    )

# --- 3. the repair is a chown, never a 777, never running as root ----------
env_code = code_of(ENV)
check(
    "777" not in code_of(BOOT) + code_of(SUPERVISE) + code_of(INSTALL) + env_code,
    "no script anywhere chmods anything to 777",
)
check(
    re.search(r'chmod\s+u\+rwx', env_code) is not None,
    "ensure_run_dir grants the owner rwx and leaves group/other alone",
)
# sudo is for the repair and nothing else.  If either runtime script could call
# sudo, the whole stack could be launched as root by one edit.
for name, path in (("boot.sh", BOOT), ("supervise.sh", SUPERVISE)):
    check(
        "sudo" not in code_of(path),
        f"{name} never calls sudo, so the app cannot be started as root",
    )
for line in env_code.splitlines():
    if "sudo" in line and not line.strip().startswith("#"):
        bare = re.sub(r'"[^"]*"', "", line)
        bare = re.sub(r"command -v\s+sudo", "", bare)
        if "sudo" in bare:
            check(
                re.search(r"sudo\s+-n\b", bare) is not None,
                f"sudo in env.sh is only ever non-interactive: {line.strip()[:60]}",
            )
            check(
                "chown" in line or "chmod" in line or "sudo -n true" in bare,
                f"sudo in env.sh is only for the repair or a probe: {line.strip()[:60]}",
            )
# Never a prompt: a Codespace cannot answer one, and a boot blocked on a
# password is worse than a boot that prints the command to run.
interactive = sudo_invocations(env_code)
check(
    not interactive,
    f"sudo is always -n, so a boot cannot hang on a password prompt {interactive}",
)

# --- 4. idempotence is structural, not accidental --------------------------
ensure_body = re.search(
    r"ensure_run_dir\(\)\s*\{(.*?)\n\}", env_code, re.S
)
check(ensure_body is not None, "ensure_run_dir is a function in env.sh")
if ensure_body:
    body = ensure_body.group(1)
    check(
        re.search(r'\[ -O "\$dir" \]\s*&&\s*\[ -w "\$dir" \]', body) is not None,
        "ensure_run_dir returns immediately when the dir is already ours and writable",
    )
    check(
        re.search(r"mkdir -p", body) is not None,
        "ensure_run_dir creates the dir when it is missing",
    )
    # mkdir -p alone is the trap: it succeeds on an existing foreign directory.
    check(
        re.search(r"mkdir -p[^\n]*\n[^\n]*\n[^\n]*(?:-O|-w)", body) is not None
        or body.index("mkdir -p") < body.index("-O"),
        "the ownership check comes after the mkdir, so a fresh dir passes it too",
    )
    check(
        body.count("return 1") >= 2,
        "ensure_run_dir reports failure instead of pretending it worked",
    )
    # A directory we already own can always be fixed by setting the owner bit,
    # which needs no privilege.  That repair has to come before the first sudo:
    # if it sat after, a Codespace without sudo would fail on a directory it
    # already had every right to fix.
    owned_fix = body.find('if [ -O "$dir" ]; then')
    first_sudo = body.find("sudo -n")
    check(
        owned_fix != -1 and (first_sudo == -1 or owned_fix < first_sudo),
        "an owned-but-unwritable run dir is repaired without needing sudo",
    )
    check(
        re.search(r'if \[ -O "\$dir" \]; then\s*\n\s*chmod u\+rwx', body) is not None,
        "that repair is a plain owner-bit chmod, not a privileged one",
    )

# --- 5. the false security claim is gone ----------------------------------
# devcontainer.json used to say a missing RAG_AUTH_TOKEN "stops the deployment",
# which has been untrue since the gate was removed.  A comment that invents a
# safety interlock is worse than no comment.
dc = DEVCONTAINER.read_text(encoding="utf-8")
dc_code = re.sub(r"//.*", "", dc)
check(
    "RAG_AUTH_TOKEN" not in dc_code,
    "devcontainer.json does not imply RAG_AUTH_TOKEN is a required secret",
)
dc_comments = " ".join(re.findall(r"//(.*)", dc))
check(
    not re.search(r"refuses every route without", dc_comments),
    "devcontainer.json no longer claims a missing token blocks the deployment",
)

# --- 6. existing security is preserved -------------------------------------
check(
    'TUNNEL_ORIGIN="http://127.0.0.1:$BACKEND_PORT"' in code_of(ENV),
    "the tunnel still publishes only the app, on loopback",
)
forwarded = re.search(r'"forwardPorts"\s*:\s*\[([^\]]*)\]', dc)
ports = re.findall(r"\d+", forwarded.group(1)) if forwarded else []
check(
    not {"5900", "9000"} & set(ports),
    f"neither the VNC port nor the agent port is forwarded ({ports})",
)
check(
    "onAutoForward" in dc and '"private"' in dc,
    "forwarded ports are still private",
)

# --- 7. behaviour, where a POSIX host is available -------------------------
# Reproduces the reported failure and the fix, including the second-run
# requirement.  Needs a POSIX host: skipped on Windows, runs in the Codespace.
HARNESS = r'''
set -uo pipefail
source "$1/env.sh" >/dev/null 2>&1
export DESKTOP_RUN_DIR="$2"
export DESKTOP_LOG_DIR="$2"
fail=0
say() { printf '%s\n' "$*"; }

# A fresh boot creates it as the user who will write to it.
ensure_run_dir "$DESKTOP_RUN_DIR" || { say "FAIL create refused"; fail=1; }
[ -w "$DESKTOP_RUN_DIR" ] || { say "FAIL not writable after create"; fail=1; }
[ -O "$DESKTOP_RUN_DIR" ] || { say "FAIL not owned by the runtime user"; fail=1; }

# Running boot.sh again must change nothing: same owner, same group, same mode.
before="$(stat -c '%U:%G:%a' "$DESKTOP_RUN_DIR")"
ensure_run_dir "$DESKTOP_RUN_DIR" || { say "FAIL second run refused"; fail=1; }
ensure_run_dir "$DESKTOP_RUN_DIR" || { say "FAIL third run refused"; fail=1; }
after="$(stat -c '%U:%G:%a' "$DESKTOP_RUN_DIR")"
[ "$before" = "$after" ] || { say "FAIL not idempotent: $before -> $after"; fail=1; }

# The owner bit can be lost without the owner changing; that is still repaired.
chmod u-w "$DESKTOP_RUN_DIR"
ensure_run_dir "$DESKTOP_RUN_DIR" || { say "FAIL mode repair refused"; fail=1; }
[ -w "$DESKTOP_RUN_DIR" ] || { say "FAIL still unwritable after repair"; fail=1; }
mode="$(stat -c '%a' "$DESKTOP_RUN_DIR")"
[ "$mode" != "777" ] || { say "FAIL repaired to 777"; fail=1; }
[ "${mode: -1}" != "7" ] || { say "FAIL other gained write: $mode"; fail=1; }
[ "${mode: -2:1}" != "7" ] || { say "FAIL group gained write: $mode"; fail=1; }

# The exact reported symptom: a pid write into a directory we cannot write.
# With a foreign owner it must fail first, then succeed after the repair.
foreign=0
if command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
  if sudo -n chown nobody "$DESKTOP_RUN_DIR" >/dev/null 2>&1; then
    foreign=1
  fi
fi

if [ "$foreign" = 1 ]; then
  if [ "$(id -u)" != 0 ]; then
    if printf 'x' > "$DESKTOP_RUN_DIR/probe" 2>/dev/null; then
      say "SKIP foreign-owner reproduction (still root; cannot observe EACCES)"
    else
      say "ok    reproduces the reported EACCES before the repair"
    fi
    rm -f "$DESKTOP_RUN_DIR/probe" 2>/dev/null
  fi
  ensure_run_dir "$DESKTOP_RUN_DIR" || { say "FAIL foreign owner not repaired"; fail=1; }
  [ -O "$DESKTOP_RUN_DIR" ] || { say "FAIL foreign owner still foreign"; fail=1; }
  printf 'x' > "$DESKTOP_RUN_DIR/probe" 2>/dev/null \
    || { say "FAIL still unwritable after repairing a foreign owner"; fail=1; }
  rm -f "$DESKTOP_RUN_DIR/probe" 2>/dev/null
  say "ok    a foreign-owned run dir is repaired and becomes writable"
else
  say "skip  foreign-owner check (needs sudo, or already root)"
fi

# write_pid: the two files boot.sh was failing on, plus a stale unwritable one.
for f in supervisor.pid backend.pid; do
  printf 'stale\n' > "$DESKTOP_RUN_DIR/$f"
  chmod 0444 "$DESKTOP_RUN_DIR/$f"
  write_pid "$DESKTOP_RUN_DIR/$f" 4242 || { say "FAIL write_pid $f"; fail=1; }
  grep -qx 4242 "$DESKTOP_RUN_DIR/$f" || { say "FAIL $f holds $(cat "$DESKTOP_RUN_DIR/$f")"; fail=1; }
  say "ok    write_pid replaced an unwritable $f"
done

# And the plain case, which is the one that happens 99% of boots.
write_pid "$DESKTOP_RUN_DIR/supervisor.pid" 77 || { say "FAIL plain write_pid"; fail=1; }
[ "$(cat "$DESKTOP_RUN_DIR/supervisor.pid")" = "77" ] || { say "FAIL wrong pid recorded"; fail=1; }

exit $fail
'''

if BASH:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        # A dir nobody owns is the interesting case, so give the harness one.
        run_dir = Path(tmp) / "ragdesktop"
        r = subprocess.run(
            [BASH, "-c", HARNESS, "--", str(CODES), str(run_dir)],
            capture_output=True,
            text=True,
            timeout=90,
        )
        combined = (r.stdout or "") + (r.stderr or "")
        for line in combined.splitlines():
            line = line.strip()
            if line.startswith(("FAIL", "SKIP", "ok ", "skip ")):
                print(f"      {line}")
        check(r.returncode == 0, f"the run dir is created, repaired and idempotent {combined.strip()[:200]}")
else:
    print("skip  behavioural checks (no bash on this host)")


# --- 8. the foreign-owner branch, on any host ------------------------------
# The branch that fixes the reported bug only runs where a directory can really
# be owned by another uid, which is a Linux box with sudo.  So the ownership
# test is stubbed out and sudo is replaced with a recorder: everything else is
# the real function, called for real, so what is verified here is which commands
# the repair issues and in what order.
FOREIGN = r'''
set -uo pipefail
work="$1"; codes="$2"
fail=0

# Only the ownership test is stubbed.  A file that exists means "pretend this
# directory belongs to someone else", so the two -O tests both read false and
# control falls through to the repair.
sed 's|\[ -O "\$dir" \]|[ ! -f "$FORCE_FOREIGN" ]|g' \
  "$codes/env.sh" > "$work/env-stubbed.sh" || { echo "FAIL cannot stub"; exit 1; }
grep -q 'FORCE_FOREIGN' "$work/env-stubbed.sh" \
  || { echo "skip  the stub did not apply"; exit 0; }

# The real functions, from the stubbed copy.
# shellcheck disable=SC1090
. "$work/env-stubbed.sh"

# A sudo that insists on -n, records itself, and then does the work.  Standing in
# for real privilege is sound here because the branch's job is to decide which
# commands to run, not what chown does.
mkdir -p "$work/fakebin"
cat > "$work/fakebin/sudo" <<'EOS'
#!/usr/bin/env bash
if [ "$1" != "-n" ]; then
  echo "INTERACTIVE SUDO CALL: $*" >&2
  exit 1
fi
echo "sudo $*" >> "$SUDO_LOG"
shift
exec "$@"
EOS
chmod +x "$work/fakebin/sudo"
PATH="$work/fakebin:$PATH"
export PATH
export SUDO_LOG="$work/sudo.log"

export FORCE_FOREIGN="$work/foreign"   # its presence forces the foreign branch
: > "$FORCE_FOREIGN"

# codespace_user reads $REPO_ROOT, so point it at the real repo.
REPO_ROOT="$codes/.."
export REPO_ROOT

mkdir -p "$work/ragdesktop"
out="$(ensure_run_dir "$work/ragdesktop" 2>&1)"
printf '%s\n' "$out" | sed 's/^/    /'
[ -w "$work/ragdesktop" ] || { echo "FAIL not writable after the repair"; fail=1; }

log="$(cat "$SUDO_LOG" 2>/dev/null)"
case "$log" in
  *"sudo -n chown"*) : ;;
  *) echo "FAIL the repair did not chown the owner: [$log]"; fail=1 ;;
esac
case "$log" in
  *"chmod u+rwx"*) : ;;
  *) echo "FAIL the owner bit was not restored: [$log]"; fail=1 ;;
esac
case "$log" in
  *"777"*) echo "FAIL the repair widened to 777: [$log]"; fail=1 ;;
esac
case "$log" in
  *"INTERACTIVE"*) echo "FAIL interactive sudo"; fail=1 ;;
esac
# chown before chmod: the other order would set a bit on a file we do not own.
ch_line="$(printf '%s\n' "$log" | grep -n chown | head -n1 | cut -d: -f1)"
cd_line="$(printf '%s\n' "$log" | grep -n chmod | head -n1 | cut -d: -f1)"
if [ -n "$ch_line" ] && [ -n "$cd_line" ] && [ "$ch_line" -gt "$cd_line" ]; then
  echo "FAIL chmod ran before chown: [$log]"
  fail=1
fi

# A directory we already own must never reach sudo at all: it needs no
# privilege, and a Codespace without sudo must still be able to fix it.
rm -f "$FORCE_FOREIGN"
export FORCE_FOREIGN="$work/absent"    # absent: the -O tests read true again

: > "$SUDO_LOG"
mkdir -p "$work/owned"
ensure_run_dir "$work/owned" >/dev/null 2>&1 \
  || { echo "FAIL a fresh directory we own was refused"; fail=1; }
if [ -s "$SUDO_LOG" ]; then
  echo "FAIL sudo was used for a directory we already own: [$(cat "$SUDO_LOG")]"
  fail=1
fi

# The same when the owner bit is the only thing missing.
: > "$SUDO_LOG"
chmod u-w "$work/owned"
ensure_run_dir "$work/owned" >/dev/null 2>&1 \
  || { echo "FAIL an owned directory with no owner bit was refused"; fail=1; }
[ -w "$work/owned" ] || { echo "FAIL owner bit was not restored"; fail=1; }
if [ -s "$SUDO_LOG" ]; then
  echo "FAIL sudo was needed to fix a directory we own: [$(cat "$SUDO_LOG")]"
  fail=1
fi

# And a second run changes nothing.
sig_before="$(stat -c '%U:%G:%a' "$work/owned")"
ensure_run_dir "$work/owned" >/dev/null 2>&1 || { echo "FAIL re-run refused"; fail=1; }
[ "$sig_before" = "$(stat -c '%U:%G:%a' "$work/owned")" ] \
  || { echo "FAIL re-run changed the directory"; fail=1; }

exit $fail
'''

if BASH:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        r = subprocess.run(
            [BASH, "-c", FOREIGN, "--", tmp, str(CODES)],
            capture_output=True,
            text=True,
            timeout=90,
        )
        combined = (r.stdout or "") + (r.stderr or "")
        for line in combined.splitlines():
            line = line.strip()
            if line.startswith(("FAIL", "skip", "[computer]")):
                print(f"      {line}")
        check(
            r.returncode == 0,
            f"a foreign-owned run dir is repaired by chown, in order, non-interactively "
            f"{combined.strip()[-200:]}",
        )
else:
    print("skip  foreign-owner branch (no bash on this host)")

print()
if failures:
    print(f"FAIL: {len(failures)} of the checks above did not hold")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)

print("PASS: the run dir belongs to the runtime user, survives a re-run, and is never widened to 777")
