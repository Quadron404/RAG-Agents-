"""A served page is not the same thing as a served app.

The app mounts ``Frontend/dist`` when that directory has an ``index.html`` and
otherwise falls back to the bundled prototype in ``backend/app/static``.  The
fallback exists so the server is never a bare 404, and it answers every request
correctly -- ``GET /`` is a 200, ``/health`` is a 200, the login endpoint is a
401 for the right reason.  It is also a completely different product: a stale
page with no login screen, no Computer view and no VNC.

That is what happened.  A frontend build failed on the Codespace, boot.sh logged
a WARNING that scrolled past, and verification reported a clean run because the
only question it asked of the UI was whether ``GET /`` returned 200.  The user
was looking at the prototype.

So three things are pinned here:

  * the fallback exists and is a real hazard, not a hypothetical
  * verification asks *which* page came back, and whether its bundle loads
  * a failed build is fatal, and the dependencies to build one are installed

Run with:  python -m tests.test_ui_served
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MAIN = REPO_ROOT / "backend" / "app" / "main.py"
STATIC = REPO_ROOT / "backend" / "app" / "static"
BOOT = REPO_ROOT / "codespace" / "boot.sh"
VERIFY = REPO_ROOT / "codespace" / "verify.sh"
INSTALL = REPO_ROOT / "codespace" / "install.sh"
DIST_INDEX = REPO_ROOT / "Frontend" / "dist" / "index.html"

failures: list[str] = []


def check(cond: bool, label: str) -> None:
    print(f"{'ok  ' if cond else 'FAIL'}  {label}")
    if not cond:
        failures.append(label)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


# --- the hazard is real ------------------------------------------------------
main = _read(MAIN)
check(bool(main), "backend/app/main.py was found")
check(
    'os.path.join(repo, "Frontend", "dist")' in main,
    "the app prefers the built UI when it exists",
)
check(
    '"static"' in main and "fall back" in main.lower(),
    "the app falls back to backend/app/static when it does not",
)
check(STATIC.is_dir(), "the fallback prototype is actually present on disk")
if STATIC.is_dir():
    proto = _read(STATIC / "index.html")
    check(bool(proto), "the fallback has its own index.html")
    # The distinction verification relies on: the real build references hashed
    # Vite assets and mounts into #root; the prototype is a standalone page.
    check(
        "/assets/index-" not in proto,
        "the fallback does not reference a Vite bundle, so the two are distinguishable",
    )

if DIST_INDEX.is_file():
    built = _read(DIST_INDEX)
    check("/assets/index-" in built, "the real build references a hashed Vite bundle")
    check('id="root"' in built, "the real build mounts into #root")

# --- verification must ask which page came back ------------------------------
verify = _read(VERIFY)
check(bool(verify), "codespace/verify.sh was found")

if verify:
    check(
        "the built UI is being served, not the fallback prototype" in verify,
        "verify.sh checks that the built UI is what is being served",
    )
    check(
        re.search(r"grep -q '/assets/index-'", verify) is not None,
        "the check identifies the real build by its hashed asset reference",
    )
    # And the bundle index.html points at has to exist: a dist left over from an
    # older build names hashed files a newer build deleted.
    check(
        "the UI bundle loads" in verify,
        "verify.sh also fetches the bundle index.html references",
    )
    check(
        'ui_asset="$(printf' in verify,
        "the asset path is extracted from the served HTML rather than assumed",
    )
    # The check has to live in the tunnel section, or it never runs when the
    # tunnel is down and the whole point is to inspect what the edge serves.
    check(
        verify.index("the built UI is being served") > verify.index("Reaching the app through the tunnel"),
        "the check is part of the through-the-tunnel section",
    )

# --- a failed build must be fatal, not a warning -----------------------------
boot = _read(BOOT)
check(bool(boot), "codespace/boot.sh was found")

if boot:
    build_block = re.search(r"if \[ ! -f \"\$REPO_ROOT/Frontend/dist/index\.html\" \]; then(.*?)\nfi\n", boot, re.S)
    check(build_block is not None, "boot.sh's build block was found")
    if build_block:
        body = build_block.group(1)
        check("exit 1" in body, "a failed frontend build aborts boot instead of warning")
        check(
            "WARNING: frontend build failed" not in body,
            "the old scroll-past warning is gone",
        )
        check(
            "frontend-build.log" in body,
            "the failure prints the build log rather than only naming it",
        )

# --- and the dependencies to build one have to exist -------------------------
install = _read(INSTALL)
check(bool(install), "codespace/install.sh was found")

if install:
    check("npm ci" in install, "install.sh installs the pinned frontend dependencies")
    check("npm run build" in install, "install.sh builds the UI rather than leaving it to boot.sh")
    check(
        re.search(r"npm run build.*?exit 1", install, re.S) is not None,
        "a failed build in install.sh is fatal too",
    )
    check(
        "command -v npm" in install,
        "install.sh installs node/npm when they are missing",
    )
    check(
        (REPO_ROOT / "Frontend" / "package-lock.json").is_file(),
        "the lockfile npm ci depends on is present",
    )

print()
if failures:
    print(f"FAIL: {len(failures)} of the checks above did not hold")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("PASS: a 200 from / is never mistaken for the app being served")
