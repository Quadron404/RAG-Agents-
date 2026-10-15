"""The ordinary agent's shell, confined to the sandbox.

A shell is the most obvious way around a browsing allowlist: ``curl`` reaches any
destination the browser is forbidden from.  So the ordinary agent's shell cannot
run on the host next to the backend; it has to run inside the same network
namespace as the restricted browser, where the only egress is the gateway.  This
service is that shell.  It is not a command allowlist -- the sandbox is the
boundary, so an arbitrary command is fine *because it cannot leave* -- and it
binds loopback, so the only caller is the backend.

Run it as ``python -m app.restricted.shell_service`` inside the sandbox.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

#: Where commands run.  The workspace, not ``/``, so a relative path in a task
#: means the same thing to the shell and to the file tools.
DEFAULT_WORKDIR = os.environ.get("WORKSPACE", os.getcwd())
MAX_OUTPUT = 8000
DEFAULT_PORT = 9112


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.split("?")[0] == "/status":
            self._json(200, {"ok": True, "service": "restricted-shell", "workdir": self.server.workdir})
        else:
            self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        if self.path.split("?")[0] != "/shell":
            self._json(404, {"ok": False, "error": "not found"})
            return
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            req = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            self._json(400, {"ok": False, "error": "invalid JSON"})
            return
        command = str(req.get("command", ""))
        timeout = int(req.get("timeout", 30) or 30)
        if not command.strip():
            self._json(200, {"output": "", "error": "empty command"})
            return
        try:
            proc = subprocess.run(
                command,
                shell=True,
                cwd=self.server.workdir,
                capture_output=True,
                text=True,
                timeout=max(1, min(timeout, 300)),
            )
        except subprocess.TimeoutExpired:
            self._json(200, {"output": "", "error": f"timed out after {timeout}s"})
            return
        output = (proc.stdout or "")
        if proc.stderr:
            output = (output + "\n[stderr]\n" + proc.stderr).strip()
        self._json(
            200,
            {
                "output": output[:MAX_OUTPUT],
                "error": "" if proc.returncode == 0 else f"exit code {proc.returncode}",
            },
        )


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, workdir: str):
        self.workdir = workdir
        super().__init__(address, _Handler)


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="restricted shell service for ordinary agents")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--workdir", default=DEFAULT_WORKDIR)
    args = parser.parse_args(argv)

    server = _Server((args.host, args.port), args.workdir)
    sys.stderr.write(f"[restricted] shell on {args.host}:{args.port} workdir={args.workdir}\n")
    sys.stderr.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
