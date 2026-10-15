"""The backend runs from exactly one environment, and that environment is complete.

The failure this pins down had three parts that each looked healthy on their own:

* requirements.txt did not declare `cryptography`, although `app.main` imports
  it through auth.deps -> auth.crypto;
* install.sh installed the requirements into the *system* interpreter with
  `pip3 --break-system-packages` and verified them there;
* boot.sh looped over `backend/.venv/bin/python` and `command -v python3`,
  accepting the first candidate that could import `uvicorn, fastapi`.

Every check in that chain passed -- those two modules really are importable in a
bare environment -- and the application still could not start.  So the
invariants asserted here are the ones that make the three parts unable to drift
apart again:

* the application's direct third-party imports are all declared in
  requirements.txt, so an environment built from that file can import the app;
* install.sh creates backend/.venv, installs into it, verifies those imports
  *there*, and never installs the backend's requirements into the system
  interpreter;
* boot.sh has exactly one candidate interpreter, probes it against every direct
  dependency before launching uvicorn, and exits with the command to run when
  the environment or its dependencies are missing;
* the agent's global extras (selenium, websocket-client, pillow) are still
  installed system-wide, because codespace/supervise.sh starts backend/vm_agent
  with the system python3 and the computer-control tests import them.

Static checks run everywhere.  The `bash -n` syntax check runs only where bash
exists; it is reported as a skip rather than a pass when it does not.
"""

from __future__ import annotations

import ast
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CODES = ROOT / "codespace"
BACKEND = ROOT / "backend"
INSTALL = CODES / "install.sh"
BOOT = CODES / "boot.sh"
REQUIREMENTS = BACKEND / "requirements.txt"

failures: list[str] = []


def check(ok: bool, label: str) -> bool:
    if ok:
        print(f"ok    {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}")
    return ok


def strip_comments(text: str) -> str:
    out = []
    for line in text.splitlines():
        if line.strip().startswith("#"):
            continue
        out.append(line)
    return "\n".join(out)


def declared_requirements() -> set[str]:
    """Top-level names in requirements.txt, extras and version pins removed."""
    names: set[str] = set()
    for raw in strip_comments(REQUIREMENTS.read_text(encoding="utf-8")).splitlines():
        m = re.match(r"([A-Za-z0-9][A-Za-z0-9._-]*)", raw.strip())
        if m:
            names.add(m.group(1).lower())
    return names


def app_direct_deps() -> set[str]:
    """Third-party names imported at module level anywhere under backend/app.

    Deliberately *module* level: selenium and websocket-client are imported
    inside functions in restricted/browser_service.py and vm_agent, which is why
    they are not requirements of the backend and are installed separately for
    the agent.  `uvicorn` is added by hand below because the app is started as
    `python -m uvicorn app.main:app` and only reaches `import uvicorn` from
    main.py's entry point, so it never shows up at module level either.
    """
    found: set[str] = set()
    for path in sorted((BACKEND / "app").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    found.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                found.add(node.module.split(".")[0])
    std = set(sys.stdlib_module_names)
    found = {n for n in found if n not in std and not n.startswith("app")}
    return found | {"uvicorn"}


def probe_imports(script_text: str, var: str = "VENV_PY") -> set[str]:
    """Modules named in a `-c "import a, b"` probe run by `$var`."""
    mods: set[str] = set()
    for line in script_text.splitlines():
        if f"${var}" not in line:
            continue
        m = re.search(r'-c\s+"import ([^"]+)"', line)
        if not m:
            continue
        for part in m.group(1).split(","):
            mods.add(part.strip().split()[0].split(".")[0])
    return mods


def exits_after(text: str, needle: str, window: int = 10) -> bool:
    """True when `needle` is followed by an `exit 1` in the next few lines."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if needle in line:
            return any(l.strip().startswith("exit 1") for l in lines[i : i + window])
    return False


REQUIRED = app_direct_deps()
declared = declared_requirements()
install_text = strip_comments(INSTALL.read_text(encoding="utf-8"))
boot_text = strip_comments(BOOT.read_text(encoding="utf-8"))

print(f"required: {' '.join(sorted(REQUIRED))}")

# --- 1. requirements.txt declares everything the app imports -----------------
missing = sorted(REQUIRED - declared)
check(
    not missing,
    "requirements.txt declares every direct dependency of backend/app"
    + (f" (missing: {', '.join(missing)})" if missing else ""),
)
check(
    "cryptography" in declared,
    "requirements.txt declares cryptography (app.main -> auth.deps -> auth.crypto)",
)

# --- 2. install.sh builds and verifies the venv, not the system interpreter ---
check(
    re.search(r"python3\s+-m\s+venv\s+\"?\$VENV_DIR", install_text) is not None,
    "install.sh creates backend/.venv with `python3 -m venv`",
)
check(
    re.search(r"\$VENV_PY[^\n]*-m pip install[^\n]*requirements\.txt", install_text)
    is not None,
    "install.sh installs requirements.txt into $VENV_PY",
)
system_installs = [
    ln
    for ln in install_text.splitlines()
    if "pip3 install" in ln and "requirements.txt" in ln
]
check(
    not system_installs,
    "install.sh never installs the backend's requirements into the system interpreter",
)
check(
    exits_after(install_text, "-m pip install --quiet -r", window=8),
    "install.sh exits non-zero when the requirements fail to install",
)
check(
    exits_after(install_text, "python3 -m venv", window=6),
    "install.sh exits non-zero when the virtual environment cannot be created",
)
install_probe = probe_imports(install_text)
check(
    not (REQUIRED - install_probe),
    "install.sh verifies every direct dependency in $VENV_PY"
    + (f" (unverified: {', '.join(sorted(REQUIRED - install_probe))})"
       if (REQUIRED - install_probe) else ""),
)

# The agent is deliberately NOT moved into the venv: supervise.sh launches
# backend/vm_agent with the system python3, and the computer-control tests
# import selenium, websocket-client and PIL.  Dropping this line would break
# Chrome and the screen rather than tidy anything up.
check(
    any(
        "pip3 install" in ln
        and all(p in ln for p in ("websocket-client", "selenium", "pillow"))
        for ln in install_text.splitlines()
    ),
    "install.sh still installs the agent's extras for the system python3",
)

# --- 3. boot.sh has one interpreter, and checks it before launching ----------
check(
    re.search(r'^VENV_PY="\$REPO_ROOT/backend/\.venv/bin/python"', boot_text, re.M)
    is not None,
    "boot.sh pins the backend interpreter to backend/.venv/bin/python",
)
check(
    "command -v python3" not in boot_text,
    "boot.sh has no system-python fallback",
)
check(
    "for cand in" not in boot_text,
    "boot.sh does not loop over candidate interpreters",
)
check(
    re.search(r'^BACKEND_PY="\$VENV_PY"', boot_text, re.M) is not None,
    "boot.sh launches uvicorn from that one interpreter",
)
boot_probe = probe_imports(boot_text)
check(
    not (REQUIRED - boot_probe),
    "boot.sh probes every direct dependency before starting"
    + (f" (unprobed: {', '.join(sorted(REQUIRED - boot_probe))})"
       if (REQUIRED - boot_probe) else ""),
)
check(
    install_probe == boot_probe,
    "install.sh and boot.sh verify the same import list",
)
probe_at = boot_text.find('-c "import')
launch_at = boot_text.find("-m uvicorn")
check(
    probe_at != -1 and launch_at != -1 and probe_at < launch_at,
    "boot.sh verifies the dependencies before launching uvicorn",
)
check(
    exits_after(boot_text, 'if [ ! -x "$VENV_PY" ]', window=6),
    "boot.sh exits with an actionable error when backend/.venv is missing",
)
check(
    exits_after(boot_text, '-c "import', window=8),
    "boot.sh exits with an actionable error when the venv's dependencies are missing",
)
check(
    "codespace/install.sh" in boot_text,
    "boot.sh tells the operator to run codespace/install.sh when the venv is absent",
)

# --- 4. the scripts still parse ---------------------------------------------
BASH = shutil.which("bash")
if BASH:
    for script in (INSTALL, BOOT):
        r = subprocess.run(
            [BASH, "-n", str(script)], capture_output=True, text=True, timeout=60
        )
        detail = ((r.stdout or "") + (r.stderr or "")).strip().replace("\n", " | ")
        check(
            r.returncode == 0,
            f"{script.name} parses under bash -n"
            + ("" if r.returncode == 0 else f" ({detail[:160]})"),
        )
else:
    print("skip  bash -n syntax check (no bash on this host)")

print()
if failures:
    print(f"FAIL: {len(failures)} of the checks above did not hold")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)

print("PASS: the backend runs from one environment, declared and verified end to end")
