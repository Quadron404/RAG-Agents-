from __future__ import annotations

import base64
import json
import os
import re
import queue
import shutil
import socket
import threading
import time
from pathlib import Path
from typing import Optional

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote
import mimetypes

def _default_workspace() -> str:
    """Where the agent's files live when WORKSPACE is not set.

    Codespaces mount the repo under /workspaces/<name> and that volume is what
    survives a stop/start, so it is preferred over the old guest path.  A
    developer with their own /workspace keeps using it.
    """
    if os.name == "nt":
        return "C:\\workspace"
    for base in ("/workspaces", "/workspace"):
        try:
            entries = [e for e in os.listdir(base) if os.path.isdir(os.path.join(base, e))]
        except OSError:
            continue
        if base == "/workspace":
            return base
        if len(entries) == 1:
            return os.path.join(base, entries[0])
        if entries:
            return os.path.join(base, sorted(entries)[0])
    return "/workspace"


WORKSPACE = os.environ.get("WORKSPACE") or _default_workspace()
SCREEN_FILE = os.path.join(WORKSPACE, ".screen.png")

os.makedirs(WORKSPACE, exist_ok=True)

MAX_OUTPUT = 8000
MAX_IMAGE_CHARS = 400_000

_browser_lock = threading.Lock()
_browser_state = {"driver": None}


def run_shell(command: str, cwd: str, timeout: int) -> dict:
    import subprocess
    try:
        r = subprocess.run(
            command, shell=True, cwd=cwd, capture_output=True, text=True, timeout=timeout
        )
        out = (r.stdout or "").strip()
        if r.stderr:
            out = (out + "\n[stderr]\n" + r.stderr.strip()).strip()
        return {
            "ok": r.returncode == 0,
            "output": out[: MAX_OUTPUT],
            "truncated": len(out) > MAX_OUTPUT,
            "code": r.returncode,
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "output": "", "error": f"timeout after {timeout}s"}


def safe_path(base: str, path: str) -> str:
    if os.path.isabs(path):
        return path
    candidate = os.path.join(base, path)
    root = os.path.abspath(base)
    if not os.path.abspath(candidate).startswith(root):
        return os.path.join(base, Path(path).name)
    return candidate


def exec_tool(tool: str, args: dict, timeout: int) -> dict:
    try:
        if tool == "shell":
            return run_shell(str(args.get("command", "")), WORKSPACE, timeout)
        if tool == "read_file":
            path = safe_path(WORKSPACE, str(args.get("path", "")))
            if not os.path.exists(path):
                return {"ok": False, "error": f"file not found: {path}"}
            ext = Path(path).suffix.lower()
            content = ""
            if ext in (".docx", ".docm"):
                content = _docx_text(path)
            elif ext == ".pdf":
                content = _pdf_text(path)
            else:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()
            return {"ok": True, "output": content[: MAX_OUTPUT], "truncated": len(content) > MAX_OUTPUT, "mime": mimetypes.guess_type(path)[0]}
        if tool == "write_file":
            path = safe_path(WORKSPACE, str(args.get("path", "")))
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(str(args.get("content", "")))
            return {"ok": True, "output": "ok"}
        if tool == "list_dir":
            path = safe_path(WORKSPACE, str(args.get("path", ".")))
            if not os.path.exists(path):
                return {"ok": False, "error": f"path not found: {path}"}
            entries = []
            for entry in sorted(os.listdir(path)):
                full = os.path.join(path, entry)
                import time as _time
                try:
                    mtime = int(os.path.getmtime(full))
                except Exception:
                    mtime = 0
                ts = _time.strftime("%Y-%m-%d %H:%M", _time.localtime(mtime))
                if os.path.isdir(full):
                    entries.append(f"dir\t{ts}\t{entry}")
                else:
                    entries.append(f"file\t{os.path.getsize(full)}\t{ts}\t{entry}")
            return {"ok": True, "output": "\n".join(entries) or "(empty)"}
        if tool in ("browser_screenshot", "browser"):
            return browser(args, timeout)
        return {"ok": False, "error": f"unknown tool: {tool}"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _find_chromium() -> Optional[str]:
    """Locate the real browser binary.

    Google Chrome is the target and is checked first; the Chromium packages are
    kept as a fallback so a slim image without Chrome still gets a screen.  An
    explicit CHROME_BINARY wins over discovery.
    """
    override = os.environ.get("CHROME_BINARY", "").strip()
    if override and os.path.exists(override):
        return override
    for cand in (
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/opt/google/chrome/google-chrome",
        "/usr/bin/chromium-browser",
        "/usr/bin/chromium",
    ):
        if os.path.exists(cand):
            return cand
    return shutil.which("google-chrome") or shutil.which("chromium")


def _ensure_browser() -> dict:
    with _browser_lock:
        if _browser_state["driver"] is not None:
            return {"ok": True}
        try:
            from selenium import webdriver
            from selenium.webdriver.chrome.options import Options
            from selenium.webdriver.chrome.service import Service
        except Exception:
            return {
                "ok": False,
                "error": "selenium not installed in VM (pip install --break-system-packages selenium)",
            }
        opts = Options()
        opts.add_argument("--headless=new")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--disable-gpu")
        opts.add_argument("--window-size=1280,800")
        opts.binary_location = _find_chromium()
        driver_path = "/usr/bin/chromedriver" if os.path.exists("/usr/bin/chromedriver") else "chromedriver"
        try:
            driver = webdriver.Chrome(service=Service(driver_path), options=opts)
        except Exception:
            driver = webdriver.Chrome(options=opts)
        driver.set_page_load_timeout(90)
        _browser_state["driver"] = driver
        return {"ok": True}


def _screenshot_embed(driver) -> dict:
    os.makedirs(WORKSPACE, exist_ok=True)
    from selenium.webdriver.common.by import By
    try:
        width = driver.execute_script("return document.body.scrollWidth")
        height = driver.execute_script("return document.body.scrollHeight")
        if width and height:
            driver.set_window_size(min(max(width, 800), 2000), min(max(height, 600), 2000))
    except Exception:
        pass
    png = driver.get_screenshot_as_png()
    with open(SCREEN_FILE, "wb") as f:
        f.write(png)
    b64 = base64.b64encode(png).decode()
    shrunk = b64
    if len(shrunk) > MAX_IMAGE_CHARS:
        shrunk = f"[base64 truncated; full image is {len(b64)} chars] " + shrunk[: MAX_IMAGE_CHARS]
    return {"path": SCREEN_FILE, "image": shrunk}


def browser(args: dict, timeout: int) -> dict:
    import subprocess
    payload = {**args, "timeout": timeout}
    with _browser_lock:
        try:
            r = subprocess.run(
                ["python3", "/opt/ragagent/webbrowser.py", json.dumps(payload)],
                capture_output=True, text=True, timeout=min(timeout + 20, 120),
            )
            try:
                return json.loads(r.stdout.strip().splitlines()[-1])
            except Exception:
                return {"ok": False, "error": (r.stderr or r.stdout or "browser failed")[:800]}
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": f"browser timed out after {timeout}s"}


#: Chrome's DevTools port.  The same value the rest of the stack already uses
#: (`CHROME_DEBUG_PORT`), so the CDP helpers below talk to the browser this
#: process itself started rather than to a hardcoded 9222 that only happens to
#: match the Codespace.  Two browsers cannot share one debug port, and the
#: failure mode without this is silent: every CDP call returns "chrome not
#: running on debug port" while Chrome is plainly running.
CDP_DEBUG_PORT = int(os.environ.get("CHROME_DEBUG_PORT", "9222"))


def _cdp_port_open() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", CDP_DEBUG_PORT), timeout=0.4):
            return True
    except Exception:
        return False


def _cdp_page_url() -> str:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{CDP_DEBUG_PORT}/json", timeout=2) as r:
            targets = json.loads(r.read().decode("utf-8", "ignore"))
    except Exception:
        return ""
    for t in targets or []:
        if t.get("type") == "page":
            return t.get("url") or ""
    return ""


def _cdp_target_wsurl() -> Optional[str]:
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{CDP_DEBUG_PORT}/json", timeout=2) as r:
            targets = json.loads(r.read().decode("utf-8", "ignore"))
    except Exception:
        return None
    for t in targets or []:
        if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
            return t["webSocketDebuggerUrl"]
    return None


def _cdp_connect(wsurl: str):
    import websocket
    # `suppress_origin`: Chrome refuses a DevTools WebSocket that carries an
    # `Origin` header unless it was started with `--remote-allow-origins`, and
    # it answers with a bare 403 that the callers above this swallow into an
    # empty url, an empty title and "the X display is not running".  Sending no
    # origin at all is accepted by Chrome whether or not the flag was passed.
    ws = websocket.create_connection(wsurl, timeout=10, suppress_origin=True)
    ws.settimeout(0.2)
    return ws


def _cdp_send(ws, mid: int, method: str, params: dict | None = None) -> None:
    ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))


_cdp_cmd_lock = threading.Lock()
_cdp_in_ws_obj = None
_cdp_in_wsurl = None
_cdp_seq = 0

# Input must not execute inside the HTTP request thread.  A CDP command can
# briefly wait for Chrome or reconnect; doing that synchronously made browser
# wheel events pile up behind a 3-10 second request timeout.  This small FIFO
# keeps button/key ordering, while collapsing only replaceable motion events.
_input_cv = threading.Condition()
_input_events = []
_input_worker_started = False


def _enqueue_cdp_input(req: dict) -> None:
    global _input_worker_started
    atype = str(req.get("type", ""))
    with _input_cv:
        if atype == "move" and _input_events and _input_events[-1].get("type") == "move":
            _input_events[-1] = req
        elif atype in ("wheel", "scroll") and _input_events and _input_events[-1].get("type") in ("wheel", "scroll"):
            prev = _input_events[-1]
            prev["dx"] = int(prev.get("dx", 0)) + int(req.get("dx", 0))
            prev["dy"] = int(prev.get("dy", 0)) + int(req.get("dy", 0))
            prev["x"], prev["y"] = req.get("x", prev.get("x", 0)), req.get("y", prev.get("y", 0))
        else:
            if len(_input_events) >= 256:
                # Prefer dropping stale motion; never drop a button or key.
                drop = next((i for i, item in enumerate(_input_events) if item.get("type") in ("move", "wheel", "scroll")), 0)
                _input_events.pop(drop)
            _input_events.append(dict(req))
        _input_cv.notify()
        if not _input_worker_started:
            _input_worker_started = True
            threading.Thread(target=_cdp_input_worker, name="cdp-input", daemon=True).start()


def _cdp_input_worker() -> None:
    while True:
        with _input_cv:
            while not _input_events:
                _input_cv.wait()
            req = _input_events.pop(0)
        try:
            _cdp_input(req)
        except Exception:
            pass


def _cdp_in_reset():
    global _cdp_in_ws_obj, _cdp_in_wsurl
    try:
        if _cdp_in_ws_obj is not None:
            try:
                _cdp_in_ws_obj.close()
            except Exception:
                pass
    finally:
        _cdp_in_ws_obj = None
        _cdp_in_wsurl = None


def _cdp_in_ws():
    global _cdp_in_ws_obj, _cdp_in_wsurl
    if _cdp_in_ws_obj is not None and getattr(_cdp_in_ws_obj, "connected", False):
        return _cdp_in_ws_obj
    _cdp_in_reset()
    wsurl = _cdp_target_wsurl()
    if not wsurl:
        return None
    ws = _cdp_connect(wsurl)
    _cdp_in_ws_obj = ws
    _cdp_in_wsurl = wsurl
    return ws


def _cdp_cmd(method: str, params: dict | None = None, wait: float = 3.0,
             want_result: bool = False) -> dict:
    """Send one CDP command over a persistent websocket (auto-reconnect once).

    `want_result` hands back the command's `result` payload.  Chrome stops
    replying on a *second* connection to a page target once another client is
    attached, so a command that needs data has to travel on the same socket the
    input worker owns rather than on a connection of its own.
    """
    import websocket
    global _cdp_seq
    for attempt in range(2):
        try:
            with _cdp_cmd_lock:
                ws = _cdp_in_ws()
                if ws is None:
                    return {"ok": False, "error": "chrome not running on debug port"}
                _cdp_seq += 1
                mid = _cdp_seq
                ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
                ws.settimeout(wait)
                deadline = time.time() + wait
                while time.time() < deadline:
                    try:
                        raw = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        _cdp_in_reset()
                        if attempt == 0:
                            raise websocket.WebSocketTimeoutException("cdp recv timeout, retrying once")
                        return {"ok": False, "error": "cdp recv timeout"}
                    except Exception as exc:
                        _cdp_in_reset()
                        raise exc
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        continue
                    if msg.get("id") == mid:
                        if msg.get("error"):
                            return {"ok": False, "error": str(msg["error"])}
                        return {"ok": True, "result": msg.get("result") or {}} if want_result else {"ok": True}
                    if msg.get("method") == "Inspector.detached":
                        _cdp_in_reset()
                        raise RuntimeError("cdp detached")
                return {"ok": False, "error": "cdp no reply"}
        except Exception as exc:
            if attempt == 1:
                return {"ok": False, "error": f"cdp failed: {exc}"}
            time.sleep(0.2)
    return {"ok": False, "error": "cdp failed"}


def _cdp_input(req: dict) -> dict:
    """Dispatch real CDP input through a single persistent CDP websocket."""
    atype = str(req.get("type", ""))
    x = int(req.get("x", 0))
    y = int(req.get("y", 0))
    button = str(req.get("button", "left"))
    if button not in ("left", "middle", "right", "back", "forward"):
        button = "left"
    buttons = int(req.get("buttons", 0))
    try:
        if atype == "move":
            _cdp_cmd("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
        elif atype in ("down", "mousedown"):
            _cdp_cmd("Input.dispatchMouseEvent", {
                "type": "mousePressed", "x": x, "y": y, "button": button,
                "buttons": buttons or (2 if button == "right" else 4 if button == "middle" else 1), "clickCount": 1,
            })
        elif atype in ("up", "mouseup"):
            _cdp_cmd("Input.dispatchMouseEvent", {
                "type": "mouseReleased", "x": x, "y": y, "button": button,
                "buttons": 0, "clickCount": 1,
            })
        elif atype == "click":
            click_button = button
            click_buttons = buttons or (2 if click_button == "right" else 4 if click_button == "middle" else 1)
            _cdp_cmd("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
            _cdp_cmd("Input.dispatchMouseEvent", {
                "type": "mousePressed", "x": x, "y": y, "button": click_button,
                "buttons": click_buttons, "clickCount": 1,
            })
            _cdp_cmd("Input.dispatchMouseEvent", {
                "type": "mouseReleased", "x": x, "y": y, "button": click_button,
                "buttons": 0, "clickCount": 1,
            })
        elif atype in ("scroll", "wheel"):
            _cdp_cmd("Input.dispatchMouseEvent", {
                "type": "mouseWheel", "x": x, "y": y,
                "deltaX": int(req.get("dx", 0)), "deltaY": int(req.get("dy", 0)),
            })
        elif atype == "text":
            _cdp_cmd("Input.insertText", {"text": str(req.get("text", ""))})
        elif atype == "enter":
            _cdp_cmd("Input.dispatchKeyEvent", {
                "type": "keyDown", "key": "Enter", "code": "Enter",
                "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13, "text": "\r",
            })
            _cdp_cmd("Input.dispatchKeyEvent", {
                "type": "keyUp", "key": "Enter", "code": "Enter",
                "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13,
            })
        elif atype == "key":
            key = str(req.get("key", "Enter"))
            code = str(req.get("code", key))
            vk = {"Backspace": 8, "Tab": 9, "Enter": 13, "Escape": 27}.get(key, 0)
            modifiers = (1 if req.get("altKey") else 0) | (2 if req.get("ctrlKey") else 0) | (4 if req.get("metaKey") else 0) | (8 if req.get("shiftKey") else 0)
            text = str(req.get("text", ""))
            _cdp_cmd("Input.dispatchKeyEvent", {
                "type": "keyDown", "key": key, "code": code,
                "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk,
                "modifiers": modifiers, **({"text": text} if text else {}),
            })
            _cdp_cmd("Input.dispatchKeyEvent", {
                "type": "keyUp", "key": key, "code": code,
                "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk,
                "modifiers": modifiers,
            })
        elif atype == "goto":
            url = str(req.get("url", "")).strip()
            if url:
                if _cdp_page_url().strip().lower().startswith("data:"):
                    _cdp_cmd("Page.navigate", {"url": "about:blank"})
                    time.sleep(0.35)
                _cdp_cmd("Page.navigate", {"url": url})
        elif atype in ("back", "forward", "reload"):
            if atype == "reload":
                _cdp_cmd("Page.reload", {"ignoreCache": True})
            else:
                _cdp_cmd("Runtime.evaluate", {"expression": f"history.{atype}()"})
        else:
            return {"ok": False, "error": f"unknown input type: {atype}"}
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": f"cdp input failed: {exc}"}


def _cdp_screencast_frames():
    """Yield jpeg base64 frame (str) for every Page.screencastFrame event."""
    import websocket
    ws = None
    while True:
        try:
            if ws is None:
                wsurl = _cdp_target_wsurl()
                if not wsurl:
                    time.sleep(1.0)
                    continue
                ws = _cdp_connect(wsurl)
                _cdp_send(ws, 1, "Page.enable")
                _cdp_send(ws, 2, "Emulation.setDeviceMetricsOverride", {
                    "width": 1280, "height": 800, "deviceScaleFactor": 0, "mobile": False,
                })
                _cdp_send(ws, 3, "Page.startScreencast", {
                    "format": "jpeg", "quality": 78, "maxWidth": 1280, "maxHeight": 800,
                    "everyNthFrame": 1,
                })
            raw = ws.recv()
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            method = msg.get("method", "")
            if method == "Page.screencastFrame":
                params = msg.get("params") or {}
                data = params.get("data", "")
                sid = params.get("sessionId")
                if sid is not None:
                    try:
                        _cdp_send(ws, 0, "Page.screencastFrameAck", {"sessionId": sid})
                    except Exception:
                        pass
                b64 = data.split(",", 1)[-1] if "," in data else data
                if b64:
                    yield b64
            elif method == "Inspector.detached":
                raise RuntimeError("detached")
        except Exception:
            try:
                if ws:
                    ws.close()
            except Exception:
                pass
            ws = None
            time.sleep(1.0)


_cdp_subs = threading.Lock()
_cdp_broadcast_t = None


def _cdp_broadcast_loop():
    while True:
        try:
            if not _cdp_target_wsurl():
                time.sleep(1.0)
                continue
            for b64 in _cdp_screencast_frames():
                with _cdp_subs:
                    for q in list(_stream_queues):
                        try:
                            q.put_nowait(b64)
                        except Exception:
                            # A slow viewer must never make newer frames wait
                            # behind stale screenshots.
                            try:
                                q.get_nowait()
                                q.put_nowait(b64)
                            except Exception:
                                pass
        except Exception:
            time.sleep(1.0)


_stream_queues = []


def _cdp_stream_start():
    global _cdp_broadcast_t
    with _cdp_broadcast_lock:
        if _cdp_broadcast_t is None or not _cdp_broadcast_t.is_alive():
            _cdp_broadcast_t = threading.Thread(target=_cdp_broadcast_loop, daemon=True)
            _cdp_broadcast_t.start()


_cdp_broadcast_lock = threading.Lock()


class _JpegSplitter:
    """Split an mjpeg byte stream into whole JPEG frames by SOI/EOI markers."""

    def __init__(self):
        self.buf = b""

    def feed(self, data: bytes) -> list:
        self.buf += data
        frames = []
        while True:
            start = self.buf.find(b"\xff\xd8\xff")
            if start < 0:
                self.buf = self.buf[-3:]
                break
            end = self.buf.find(b"\xff\xd9", start + 3)
            if end < 0:
                self.buf = self.buf[start:]
                break
            frames.append(self.buf[start:end + 2])
            self.buf = self.buf[end + 2:]
        return frames


_desktop_subs = []
_desktop_subs_lock = threading.Lock()
_desktop_latest_b64 = ""
_desktop_broadcast_t = None
_desktop_broadcast_lock = threading.Lock()
# DESKTOP_DISPLAY / DESKTOP_SIZE are defined by the supervisor section below.


def _desktop_capture_loop():
    global _desktop_latest_b64
    proc = None
    splitter = _JpegSplitter()
    last_frame = b""
    last_sent = 0.0
    while True:
        try:
            with _desktop_subs_lock:
                nsubs = len(_desktop_subs)
            if nsubs <= 0:
                if proc is not None:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    proc = None
                time.sleep(0.5)
                continue
            if proc is None:
                import subprocess as _sp
                try:
                    proc = _sp.Popen(
                        [
                            "ffmpeg", "-loglevel", "error", "-nostdin",
                            "-f", "x11grab", "-framerate", "60",
                            "-video_size", DESKTOP_SIZE,
                            "-draw_mouse", "0",
                            "-i", DESKTOP_DISPLAY,
                            "-f", "image2pipe", "-c:v", "mjpeg",
                            "-q:v", "6", "-r", "60", "pipe:1",
                        ],
                        stdout=_sp.PIPE, stderr=_sp.DEVNULL, bufsize=0,
                    )
                except Exception:
                    proc = None
                    time.sleep(1.0)
                    continue
            raw = proc.stdout.read(131072)
            if not raw:
                try:
                    proc.kill()
                except Exception:
                    pass
                proc = None
                time.sleep(1.0)
                continue
            for frame in splitter.feed(raw):
                if len(frame) < 120:
                    continue
                if frame == last_frame:
                    continue
                _now = time.time()
                _dt = (1.0 / 60.0) - (_now - last_sent)
                if _dt > 0.01:
                    time.sleep(min(_dt, 0.25))
                b64 = base64.b64encode(frame).decode("ascii")
                last_sent = time.time()
                last_frame = frame
                _desktop_latest_b64 = b64
                with _desktop_subs_lock:
                    for q in list(_desktop_subs):
                        try:
                            q.put_nowait(b64)
                        except Exception:
                            try:
                                q.get_nowait()
                                q.put_nowait(b64)
                            except Exception:
                                pass
        except Exception:
            time.sleep(0.5)


def _desktop_stream_start():
    global _desktop_broadcast_t
    with _desktop_broadcast_lock:
        if _desktop_broadcast_t is None or not _desktop_broadcast_t.is_alive():
            _desktop_broadcast_t = threading.Thread(target=_desktop_capture_loop, daemon=True)
            _desktop_broadcast_t.start()


# ---------------------------------------------------------------------------
# Desktop supervisor — the REAL graphical session behind the "Live Screen"
#
#   Xvfb :99 + real Google Chrome (normal window, full UI)
#     -> x11vnc  (RFB server, 127.0.0.1:5900 only)
#       -> websockify  127.0.0.1:6080
#         -> cloudflared tunnel -> Cloudflare Access -> the user's browser
#           -> noVNC embedded in the Computer view
#
# This process only *supervises* those processes.  It never captures,
# re-encodes or forwards frames, so the screen the user sees is always the
# genuine framebuffer, whatever Chrome happens to be drawing (including
# nothing at all when Chrome has crashed).
#
# Deliberate choices:
#   * PIDs are tracked in files under RUN_DIR instead of `pkill -f`.
#     A pattern like "remote-debugging-port" also matches the shell that this
#     daemon spawns, so pkill -f made the agent kill its own request thread.
#   * Chrome is launched *headful* on that display and keeps
#     --remote-debugging-port, so the window on screen and the CDP target that
#     the automation tools drive are the same process.
#   * x11vnc is pinned to 127.0.0.1 with -localhost.  RFB is deliberately not
#     published: the only route out is the authenticated Cloudflare path.
# ---------------------------------------------------------------------------

DESKTOP_DISPLAY = os.environ.get("DESKTOP_DISPLAY", ":99")
DESKTOP_SIZE = os.environ.get("DESKTOP_SIZE", "1365x768")
DESKTOP_DEPTH = int(os.environ.get("DESKTOP_DEPTH", "24"))
DESKTOP_WM = os.environ.get("DESKTOP_WM", "fluxbox")


def _log(message: str) -> None:
    """One line to stdout, which is what supervises.sh captures into agent.log."""
    print(f"[agent] {message}", flush=True)


# The real X screen size, cached.  None means "not measured yet"; a tuple means
# X has been asked and its answer is what the coordinate contract is built on.
# See _display_geometry for why the request is not trusted.
_GEOMETRY_CACHE = None
VNC_PORT = int(os.environ.get("VNC_PORT", "5900"))
# The RFB -> WebSocket hop.  The app proxies /websockify here after checking the
# session cookie, so this port is the only way the framebuffer leaves the
# machine and it is bound to loopback like every other one.
WEBSOCKIFY_PORT = int(os.environ.get("WEBSOCKIFY_PORT", "6080"))
NOVNC_WEB = os.environ.get("NOVNC_WEB", "/usr/share/novnc")
VNC_PASSWORD = os.environ.get("VNC_PASSWORD", "")
CHROME_PROFILE = os.environ.get("CHROME_PROFILE", "/workspaces/chrome-profile")
CHROME_DEBUG_PORT = int(os.environ.get("CHROME_DEBUG_PORT", str(CDP_DEBUG_PORT)))
RUN_DIR = os.environ.get("DESKTOP_RUN_DIR", "/run/ragdesktop")
LOG_DIR = os.environ.get("DESKTOP_LOG_DIR", "/var/log/ragdesktop")
# Chrome opens as a normal browser window sized to fill the display, so the tab
# strip, new-tab button and address bar are all on screen and clickable.  It is
# not kiosk mode: kiosk hides exactly that UI.  START_URL is the first page.
START_URL = os.environ.get("CHROME_START_URL", "https://x.com")

_desktop_lock = threading.RLock()
_desktop_state = {"x_epoch": 0, "started_at": 0.0, "x_started_at": 0.0, "restarts": {}}
_desktop_spawn_lock = threading.Lock()


def _desktop_width() -> int:
    w, _ = _display_geometry()
    return w


def _desktop_height() -> int:
    _, h = _display_geometry()
    return h


def _display_geometry() -> tuple:
    """The REAL X screen size, asked of the X server itself.

    DESKTOP_SIZE is only a request.  It is what Xvfb was asked for and what
    ffmpeg is told to grab, so if the two ever disagree -- a stale env var, an X
    server left over from an earlier run at a different size -- every coordinate
    in the loop is wrong: the model is told one grid and xdotool acts on
    another.  `xdotool getdisplaygeometry` reports what X actually has, so that
    is what bounds are checked against, and the requested size is only a
    fallback for when X cannot be asked.
    """
    global _GEOMETRY_CACHE
    if _GEOMETRY_CACHE is not None:
        return _GEOMETRY_CACHE
    requested = (1365, 768)
    try:
        parts = DESKTOP_SIZE.split("x")
        requested = (int(parts[0]), int(parts[1]))
    except Exception:
        pass
    if not _x_running():
        _GEOMETRY_CACHE = requested
        return _GEOMETRY_CACHE
    try:
        r = _xdotool("getdisplaygeometry", timeout=10)
        if r.returncode == 0:
            raw = r.stdout
            nums = (raw.decode("ascii", "replace") if isinstance(raw, bytes) else str(raw)).split()
            if len(nums) == 2 and all(n.isdigit() for n in nums):
                actual = (int(nums[0]), int(nums[1]))
                if actual != requested:
                    _log(
                        f"DISPLAY GEOMETRY MISMATCH: X is {actual[0]}x{actual[1]} "
                        f"but DESKTOP_SIZE says {requested[0]}x{requested[1]}. "
                        f"Coordinates will follow X, not the request."
                    )
                _GEOMETRY_CACHE = actual
                return _GEOMETRY_CACHE
    except Exception as exc:
        _log(f"could not read the display geometry ({exc}); using DESKTOP_SIZE")
    _GEOMETRY_CACHE = requested
    return _GEOMETRY_CACHE


def _reset_display_geometry_cache() -> None:
    """Forget the cached size.  Called when X is restarted or resized."""
    global _GEOMETRY_CACHE
    _GEOMETRY_CACHE = None


def _jpeg_size(data: bytes) -> tuple:
    """The real pixel size of a JPEG, read from its own SOF marker.

    The loop used to report the size of the string in DESKTOP_SIZE and trust
    that the bytes matched.  The model is told a grid; if the image is a
    different grid then every coordinate it returns is in the wrong space, and
    the symptom is a click that misses by a ratio nobody can see.  So the
    dimensions are read out of the bytes that are actually going to be sent.
    """
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        return (0, 0)
    i = 2
    n = len(data)
    # SOF0..SOF15, skipping the three markers that share the range and are not
    # frame headers (DHT=c4, JPG=c8, DAC=cc).
    while i + 9 < n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        if marker == 0xFF:
            i += 1
            continue
        if i + 4 > n:
            break
        seg_len = (data[i + 2] << 8) | data[i + 3]
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            if i + 9 <= n:
                h = (data[i + 5] << 8) | data[i + 6]
                w = (data[i + 7] << 8) | data[i + 8]
                return (w, h)
            break
        i += 2 + seg_len
    return (0, 0)


def _pointer_position() -> tuple:
    """Where the pointer actually is, read back from the X server.

    This is the check that turns "the click probably landed" into "the pointer
    is at 700,350".  Without it a click that silently missed looks exactly like
    a click that worked and the page simply did not respond.
    """
    try:
        r = _xdotool("getmouselocation", "--shell", timeout=10)
        if r.returncode == 0:
            raw = r.stdout
            text = raw.decode("ascii", "replace") if isinstance(raw, bytes) else str(raw)
            x = y = None
            for line in text.splitlines():
                if line.startswith("X="):
                    x = int(line.split("=", 1)[1])
                elif line.startswith("Y="):
                    y = int(line.split("=", 1)[1])
            if x is not None and y is not None:
                return (x, y)
    except Exception:
        pass
    return (None, None)


def _focus_window_under_cursor() -> bool:
    """Give the keyboard focus to the window the pointer is over.

    Keystrokes go to whatever has input focus, not to whatever is on top.  A
    click sets focus through the window manager, but not when the click lands on
    a part of the page that is not focusable, and not at all on the very first
    action of a run.  Typing then goes to the wrong window and appears to do
    nothing, which reads as "keyboard control is broken".
    """
    if not DESKTOP_WM or DESKTOP_WM.lower() == "none":
        return False  # no WM means no window focus to hand around
    try:
        win = _xdotool("getwindowundercursor", timeout=10)
        if win.returncode != 0:
            return False
        raw = win.stdout
        wid = (raw.decode("ascii", "replace") if isinstance(raw, bytes) else str(raw)).strip()
        if not wid.isdigit():
            return False
        act = _xdotool("windowactivate", "--sync", wid, timeout=10)
        if act.returncode != 0:
            foc = _xdotool("windowfocus", "--sync", wid, timeout=10)
            return foc.returncode == 0
        return True
    except Exception:
        return False


def _x_socket() -> str:
    num = DESKTOP_DISPLAY.lstrip(":").split(".")[0]
    return f"/tmp/.X11-unix/X{num}"


def _pid_file(name: str) -> str:
    return os.path.join(RUN_DIR, f"{name}.pid")


def _read_pid(name: str) -> int:
    try:
        with open(_pid_file(name)) as f:
            return int(f.read().strip())
    except Exception:
        return 0


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False
    except Exception:
        return False


# Live Popen handles for the supervised processes.  Without reaping, a process
# that dies stays a zombie and os.kill(pid, 0) keeps succeeding for it, so the
# supervisor would believe a dead X server or window manager is still fine.
_desktop_procs: dict = {}


def _reap_children() -> None:
    """Reap exited children so liveness checks tell the truth."""
    for name, proc in list(_desktop_procs.items()):
        try:
            if proc.poll() is not None:
                _desktop_procs.pop(name, None)
        except Exception:
            _desktop_procs.pop(name, None)


def _alive(name: str) -> bool:
    """True when the supervised process `name` is running.

    Prefers the Popen handle (authoritative, and reaped) and falls back to the
    recorded pid for processes started before this agent booted.
    """
    proc = _desktop_procs.get(name)
    if proc is not None:
        try:
            return proc.poll() is None
        except Exception:
            return False
    return _pid_alive(_read_pid(name))


def _kill_pid(name: str, sig: int = 15) -> None:
    """Kill a supervised process by the pid we recorded, never by name match."""
    proc = _desktop_procs.pop(name, None)
    pid = _read_pid(name)
    if _pid_alive(pid):
        try:
            os.kill(pid, sig)
        except Exception:
            pass
        # Wait on the Popen handle when we own it: a child we have not reaped
        # stays visible to os.kill, so polling the pid alone would always time out.
        if proc is not None:
            for _ in range(30):
                try:
                    if proc.poll() is not None:
                        break
                except Exception:
                    break
                time.sleep(0.1)
        else:
            for _ in range(30):
                if not _pid_alive(pid):
                    break
                time.sleep(0.1)
        if proc is None and _pid_alive(pid):
            try:
                os.kill(pid, 9)
            except Exception:
                pass
    try:
        os.remove(_pid_file(name))
    except Exception:
        pass


def _port_listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except Exception:
        return False


def _clients_connected(port: int) -> int:
    """Count established connections to `port` (real viewers, not our probes)."""
    total = 0
    hexport = f"{port:04X}"
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(path) as f:
                next(f, None)
                for line in f:
                    parts = line.split()
                    if len(parts) < 4 or parts[1].split(":")[-1].upper() != hexport:
                        continue
                    if parts[3] == "01":  # ESTABLISHED
                        total += 1
        except Exception:
            continue
    return total


def _spawn(name: str, argv: list, env: Optional[dict] = None) -> int:
    os.makedirs(RUN_DIR, exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)
    import subprocess as _sp
    child_env = dict(os.environ)
    if env:
        child_env.update(env)
    log = open(os.path.join(LOG_DIR, f"{name}.log"), "ab", buffering=0)
    try:
        proc = _sp.Popen(
            argv,
            stdin=_sp.DEVNULL,
            stdout=log,
            stderr=_sp.STDOUT,
            env=child_env,
            start_new_session=True,
            close_fds=True,
        )
    finally:
        try:
            log.close()
        except Exception:
            pass
    with open(_pid_file(name), "w") as f:
        f.write(str(proc.pid))
    _desktop_procs[name] = proc
    return proc.pid


def _note_restart(name: str) -> None:
    with _desktop_lock:
        counts = _desktop_state["restarts"]
        counts[name] = int(counts.get(name, 0)) + 1
        _desktop_state["started_at"] = time.time()


def _x_running() -> bool:
    return os.path.exists(_x_socket())


# Xvfb needs a moment to create its socket.  Without this grace window a freshly
# spawned X looks dead to the next check, and the teardown below would kill the
# browser and VNC server that had only just come up.
_X_SETTLE_SECONDS = 25.0


def _x_starting() -> bool:
    started = float(_desktop_state.get("x_started_at") or 0.0)
    return bool(started) and (time.time() - started) < _X_SETTLE_SECONDS


def _ensure_x() -> None:
    if _x_running() and _alive("xvfb"):
        return
    if _x_starting():
        # Still inside the settle window: give X a chance to come up instead of
        # tearing the display stack down and starting over.
        for _ in range(20):
            if _x_running() and _alive("xvfb"):
                return
            time.sleep(0.5)
        if _x_running():
            return
    if _x_running():
        # Socket left behind by a dead Xvfb: the display is unusable.
        try:
            os.remove(_x_socket())
        except Exception:
            pass
    with _desktop_lock:
        epoch = _desktop_state["x_epoch"] + 1
        _desktop_state["x_epoch"] = epoch
        _desktop_state["x_started_at"] = time.time()
    # Everything that draws into X dies with X; clear it so it is respawned.
    for name in ("chromium", "vnc", "wm"):
        _kill_pid(name)
    _spawn(
        "xvfb",
        [
            "Xvfb", DESKTOP_DISPLAY,
            "-screen", "0", f"{DESKTOP_SIZE}x{DESKTOP_DEPTH}",
            "-ac",              # no host-based access control: x11vnc attaches as a local client
            "-nolisten", "tcp", # never accept X clients over the network
            "-noreset",
        ],
    )
    _note_restart("xvfb")
    # A new X server is a new coordinate system, so the cached size from the old
    # one is now a lie.
    _reset_display_geometry_cache()
    for _ in range(60):
        if _x_running():
            break
        time.sleep(0.25)
    time.sleep(0.5)


def _ensure_wm() -> None:
    if not DESKTOP_WM or DESKTOP_WM.lower() == "none":
        return
    if _alive("wm"):
        return
    if not _x_running():
        return
    _spawn("wm", [DESKTOP_WM], env={"DISPLAY": DESKTOP_DISPLAY})
    _note_restart("wm")


def _ensure_vnc() -> None:
    if not _x_running():
        return
    if _alive("vnc") and _port_listening(VNC_PORT):
        return
    _kill_pid("vnc")
    argv = [
        "x11vnc",
        "-display", DESKTOP_DISPLAY,
        "-rfbport", str(VNC_PORT),
        "-localhost",         # RFB answers on 127.0.0.1 only, never on 0.0.0.0
        "-forever",            # keep serving after a viewer disconnects
        "-shared",             # allow several viewers (host + tools)
        "-repeat",             # autorepeat for held keys
        "-noxdamage",          # Xvfb has no damage extension worth using
        "-nolookup",           # no name lookup on every client connect
        "-quiet",
    ]
    if VNC_PASSWORD:
        argv += ["-rfbauth", _vnc_password_file(VNC_PASSWORD)]
    else:
        # Loopback-only RFB is reachable exclusively through the local
        # websockify/tunnel chain, so it never needs its own password.
        argv += ["-nopw"]
    _spawn("vnc", argv, env={"DISPLAY": DESKTOP_DISPLAY})
    _note_restart("vnc")
    for _ in range(40):
        if _port_listening(VNC_PORT):
            break
        time.sleep(0.25)


def _vnc_password_file(password: str) -> str:
    """Write a VNC password file and return its path (obfuscated, not plaintext)."""
    import base64 as _b64
    path = os.path.join(RUN_DIR, "vncpass")
    blob = _b64.b64encode(password.encode("utf-8")).decode("ascii")
    with open(path, "w") as f:
        f.write(blob)
    os.chmod(path, 0o600)
    return path


def _chromium_argv() -> list:
    binary = _find_chromium() or "chromium"
    return [
        binary,
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-networking",
        "--disable-sync",
        "--disable-features=Translate,MediaRouter,OptimizationHints",
        "--remote-allow-origins=*",
        f"--remote-debugging-port={CHROME_DEBUG_PORT}",
        # Persistent profile: the user logs into sites by hand, so it must
        # survive restarts and land on the persistent Codespaces volume.
        f"--user-data-dir={CHROME_PROFILE}",
        # Deliberately NOT --kiosk.  Kiosk is exactly the mode that hides the tab
        # strip, the new-tab button and the address bar, which are the parts of
        # the framebuffer the user has to be able to click.  The window is sized
        # and positioned to fill the display instead, so the real Chrome UI is
        # on screen and usable.
        "--start-maximized",
        f"--window-size={_desktop_width()},{_desktop_height()}",
        "--window-position=0,0",
        START_URL,
    ]


def _ensure_chromium() -> None:
    """Keep exactly one headful Chromium alive on the real display.

    Deliberately no --headless: a headless browser paints nothing into X, so the
    VM screen would show an empty desktop while the agent still believed it was
    browsing.  The window on screen and the CDP target are the same process, so
    navigating through CDP visibly repaints the VNC framebuffer.
    """
    if not _x_running():
        return
    try:
        os.makedirs(CHROME_PROFILE, exist_ok=True)
    except Exception:
        pass
    if _port_listening(CHROME_DEBUG_PORT):
        # A browser is already serving CDP (ours or one started by the browser
        # tool).  Leave it completely alone so its state is never disturbed.
        return
    _kill_pid("chromium")
    _spawn("chromium", _chromium_argv(), env={"DISPLAY": DESKTOP_DISPLAY})
    _note_restart("chromium")
    for _ in range(80):
        if _port_listening(CHROME_DEBUG_PORT):
            break
        time.sleep(0.25)


def _desktop_status() -> dict:
    x = _x_running()
    return {
        "display": DESKTOP_DISPLAY,
        "size": DESKTOP_SIZE,
        "depth": DESKTOP_DEPTH,
        "wm": DESKTOP_WM,
        "x": x,
        "x_pid": _read_pid("xvfb"),
        "wm_alive": _alive("wm"),
        "vnc": _port_listening(VNC_PORT),
        "vnc_port": VNC_PORT,
        "vnc_pid": _read_pid("vnc"),
        "vnc_clients": _clients_connected(VNC_PORT),
        "vnc_password": bool(VNC_PASSWORD),
        # Reported so /display/status can distinguish "RFB is up" from "there is
        # actually a way in".  A healthy x11vnc with a dead 6080 is exactly the
        # failure that looked fine from the outside.
        "websockify": _port_listening(WEBSOCKIFY_PORT),
        "websockify_port": WEBSOCKIFY_PORT,
        "websockify_pid": _read_pid("websockify"),
        "chromium": _port_listening(CHROME_DEBUG_PORT),
        "chromium_pid": _read_pid("chromium"),
        "cdp_port": CHROME_DEBUG_PORT,
        "started_at": _desktop_state["started_at"],
        "restarts": dict(_desktop_state["restarts"]),
        "supervised": True,
    }


def _ensure_websockify() -> None:
    """Keep websockify alive on 127.0.0.1:6080.

    This belongs here, not only in codespace/supervise.sh.  The daemon is the
    process that owns the screen chain, and websockify is the last link in it
    (RFB 5900 -> WebSocket 6080 -> the app's /websockify).  When it was started
    only by the shell supervisor, a race between the two left 6080 down while
    everything upstream reported healthy -- x11vnc up, Chrome up, agent up, and
    no way in.

    Both the daemon and supervise.sh can start it; whichever gets there first
    wins and the other sees the port listening and does nothing.  The bind is
    written as 127.0.0.1 explicitly rather than relying on a flag, because this
    is the one port that must never appear on a public interface.
    """
    if not _x_running():
        return
    if _alive("websockify") and _port_listening(WEBSOCKIFY_PORT):
        return
    _kill_pid("websockify")
    web_root = NOVNC_WEB
    argv = [
        "websockify",
        f"--web={web_root}",
        f"127.0.0.1:{WEBSOCKIFY_PORT}",
        f"127.0.0.1:{VNC_PORT}",
    ]
    _spawn("websockify", argv)
    _note_restart("websockify")
    for _ in range(40):
        if _port_listening(WEBSOCKIFY_PORT):
            break
        time.sleep(0.25)


def _desktop_ensure() -> dict:
    """Bring the whole stack up now (used by the backend and by /tool/exec)."""
    with _desktop_spawn_lock:
        _reap_children()
        _ensure_x()
        _ensure_wm()
        _ensure_vnc()
        _ensure_websockify()
        _ensure_chromium()
    return _desktop_status()


def _desktop_restart(chromium_only: bool = False) -> dict:
    with _desktop_spawn_lock:
        if chromium_only:
            _kill_pid("chromium", 9)
            time.sleep(0.5)
        else:
            for name in ("chromium", "websockify", "vnc", "wm", "xvfb"):
                _kill_pid(name, 9)
            time.sleep(0.5)
    return _desktop_ensure()


def _desktop_stop() -> dict:
    """Tear the whole screen stack down; the supervisor brings it back on demand.

    X is left running when only the browser is the problem, because a dead X
    takes the VNC server with it and there is nothing to look at until the next
    ensure anyway.
    """
    with _desktop_spawn_lock:
        _kill_pid("chromium", 9)
    time.sleep(0.3)
    return _desktop_status()


def _desktop_supervisor_loop() -> None:
    """Keep the screen alive: if a component dies it comes back by itself."""
    while True:
        try:
            with _desktop_spawn_lock:
                _ensure_x()
                _ensure_wm()
                _ensure_vnc()
                _ensure_websockify()
                _ensure_chromium()
        except Exception:
            pass
        time.sleep(2.0)


def _docx_text(path: str) -> str:
    try:
        import zipfile
        import re as _re
        with zipfile.ZipFile(path) as z:
            xml = z.read("word/document.xml").decode("utf-8", "ignore")
        xml = _re.sub(r"<w:p[ >]", "\n<w:p ", xml)
        xml = _re.sub(r"<w:tab[ >]", "\t", xml)
        text = "".join(_re.findall(r"<w:t[^>]*>(.*?)</w:t>", xml, _re.S))
        text = _re.sub(r"</w:p[^>]*>", "\n", text)
        text = _re.sub(r"\n{3,}", "\n\n", text)
        return text.strip() or "(empty document)"
    except zipfile.BadZipFile:
        return "(not a valid .docx document)"


def _pdf_text(path: str) -> str:
    import subprocess as _sp
    try:
        r = _sp.run(["pdftotext", "-layout", path, "-"], capture_output=True, text=True, timeout=30)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return "(PDF — text extraction not available; open it to download)"


# ---------------------------------------------------------------------------
# Computer control: the primitives an AI agent drives the real browser with.
#
# Two decisions here matter more than the code.
#
# The screenshot is the X display, not the page.  CDP can capture a page
# viewport, but that image is in *viewport* coordinates while the address bar and
# tab strip are not in it at all -- so a model shown that picture would be
# reasoning about a browser it cannot see the controls of.  x11grab returns the
# same framebuffer the user is looking at over VNC, tab bar and all.
#
# The click is an X event, not a CDP one, and that is what keeps coordinates
# honest.  A CDP Input.dispatchMouseEvent takes viewport CSS pixels, so it could
# not honour a coordinate read off a full-display screenshot.  xdotool moves the
# real pointer on the real display, which is the same input path a human's mouse
# takes: Chrome receives a genuine click, and it is visibly so in the VNC
# session and in the next screenshot.
#
# Both are read-only with respect to the user's own machine.  Nothing here can
# reach anything but the local X display the agent already owns.
# ---------------------------------------------------------------------------

_CAPTURE_LOCK = threading.Lock()
_SEARCH_ENGINE = os.environ.get("COMPUTER_SEARCH_URL", "https://duckduckgo.com/?q=")


def _xdotool(*args: str, timeout: int = 10) -> tuple:
    import subprocess

    return subprocess.run(
        ["xdotool", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env={**os.environ, "DISPLAY": DESKTOP_DISPLAY},
    )


def _cdp_viewport() -> tuple:
    """(width, height, dpr) of the page, in CSS pixels, or (0, 0, 0).

    CSS pixels are the coordinate space every CDP input command uses -- a click
    at (700, 450) is 700 from the left edge of the viewport, whatever the
    display's scaling factor happens to be.
    """
    reply = _cdp_cmd("Runtime.evaluate",
                     {"expression": "JSON.stringify({w:innerWidth,h:innerHeight,"
                                    "dpr:devicePixelRatio})", "returnByValue": True},
                     wait=5.0, want_result=True)
    if not reply.get("ok"):
        return (0, 0, 0)
    try:
        value = (((reply.get("result") or {}).get("result") or {}).get("value"))
        info = json.loads(value or "{}")
        return (int(info.get("w") or 0), int(info.get("h") or 0), float(info.get("dpr") or 0))
    except Exception:
        return (0, 0, 0)


def _cdp_screenshot() -> dict:
    """One JPEG of the real Chrome window, captured over CDP.

    The X path in `_capture_display` is the production one: it captures the
    whole desktop, so anything else on the display is visible to the model too.
    It needs Xvfb, which only exists on the Codespace.  Where Chrome is running
    with its debug port open and there is no X display -- a workstation, a CI
    box -- the same real pixels are one CDP command away, and refusing there
    would make the loop unable to see a browser that is right there.

    Same pixels, one less dependency: `Page.captureScreenshot` is Chrome
    compositing its own page, not a drawing of anything the loop made up.  The
    JSON is the same shape `_capture_display` returns, so the loop cannot tell
    the two apart and needs no branch of its own.
    """
    wsurl = _cdp_target_wsurl()
    if not wsurl:
        return {"ok": False, "error": "chrome is not running on the debug port"}
    params = {"format": "jpeg", "quality": 82}
    width, height, dpr = _cdp_viewport()
    if width > 0 and height > 0:
        # Capture in CSS pixels, not device pixels.  Chrome's screenshot comes
        # back at the display's scaling factor -- 1.25 here -- while every input
        # command below is in CSS pixels, so an unscaled image would hand the
        # model a coordinate system 25% larger than the one its clicks are
        # measured in, and every click would miss by exactly that ratio.
        params["clip"] = {"x": 0, "y": 0, "width": width, "height": height,
                          "scale": round(1.0 / dpr, 4) if dpr > 0 else 1.0}
    else:
        # No viewport to ask for (a page that is still loading, say): take the
        # window and report whatever size the bytes turn out to be.
        params["captureBeyondViewport"] = False
    reply = _cdp_cmd("Page.captureScreenshot", params, wait=20.0, want_result=True)
    if not reply.get("ok"):
        return {"ok": False, "error": f"cdp screenshot failed: {reply.get('error')}"}
    data = ((reply.get("result") or {}).get("data")) or ""
    if not data:
        return {"ok": False, "error": "cdp screenshot returned no image"}
    try:
        raw = base64.b64decode(data)
    except Exception:
        return {"ok": False, "error": "cdp screenshot returned an undecodable image"}
    image_width, image_height = _jpeg_size(raw)
    if width > 0 and height > 0 and (image_width, image_height) != (width, height):
        # Loud rather than quiet: the size reported to the app is the coordinate
        # system the model is told to reason in, so a disagreement here is the
        # "every click misses by a ratio and nothing looks wrong" failure.
        _log(
            f"CDP GEOMETRY MISMATCH: page is {width}x{height} CSS px but the "
            f"screenshot is {image_width}x{image_height}. Coordinates will be off by a ratio."
        )
        width, height = image_width, image_height
    return {"ok": True, "image": data, "width": image_width or width,
            "height": image_height or height, "source": "cdp"}


def _capture_display(draw_mouse: bool = True) -> dict:
    """One JPEG of the real X display, as base64 with its pixel size.

    `-draw_mouse 1` composites the pointer into the image so the model can see
    where it last left the cursor, which is the difference between a click and a
    guess.  The live VNC stream deliberately does the opposite -- a drawn cursor
    on a 60fps stream is a constant repaint of the same pixels.
    """
    import subprocess

    if not _x_running():
        # No X display, but Chrome may still be right here on its debug port.
        # Fall back to its own pixels rather than refusing: the alternative is a
        # loop that cannot see a browser that is plainly open.
        if _cdp_target_wsurl():
            return _cdp_screenshot()
        return {"ok": False, "error": "the X display is not running"}
    # Serialised on purpose: two concurrent x11grab processes on one display is
    # the fastest way to make the agent unresponsive for no benefit, and only
    # ever one agent is driving it.
    with _CAPTURE_LOCK:
        cmd = [
            "ffmpeg", "-loglevel", "error", "-nostdin",
            "-f", "x11grab",
            "-video_size", DESKTOP_SIZE,
            "-draw_mouse", "1" if draw_mouse else "0",
            "-i", DESKTOP_DISPLAY,
            "-frames:v", "1",
            "-q:v", "3",
            "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1",
        ]
        try:
            r = subprocess.run(
                cmd,
                capture_output=True,
                timeout=25,
                env={**os.environ, "DISPLAY": DESKTOP_DISPLAY},
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "screen capture timed out"}
        except FileNotFoundError:
            return {"ok": False, "error": "ffmpeg is not installed on the remote computer"}
    if r.returncode != 0 or not r.stdout:
        return {
            "ok": False,
            "error": (r.stderr or b"capture failed").decode("utf-8", "replace")[:300],
        }
    # The size reported to the app is the size of the bytes actually being sent,
    # read out of the JPEG itself.  The model is told "these pixels are your
    # coordinate system", so that number has to describe the image in front of
    # it.  Reporting the requested size instead would let the two disagree
    # silently, which is exactly the case where every click is off by a ratio
    # and nothing anywhere looks wrong.
    jw, jh = _jpeg_size(r.stdout)
    disp_w, disp_h = _display_geometry()
    width, height = (jw, jh) if (jw and jh) else (disp_w, disp_h)
    matches = (jw, jh) == (disp_w, disp_h) if (jw and jh) else True
    if not matches:
        _log(
            f"SCREENSHOT SIZE MISMATCH: the image is {jw}x{jh} but the X display "
            f"is {disp_w}x{disp_h}. Reporting the image size, because that is what "
            f"the model will be shown. Clicks may be off by a ratio."
        )
    return {
        "ok": True,
        "image": base64.b64encode(r.stdout).decode("ascii"),
        "mime": "image/jpeg",
        "width": int(width),
        "height": int(height),
        # Reported alongside so a mismatch is visible in the app's log without
        # having to go and read the agent's.
        "display_width": int(disp_w),
        "display_height": int(disp_h),
        "geometry_matches": matches,
    }


def _computer_click(x: int, y: int) -> dict:
    """Move to the requested display pixel, activate Chrome, then left-click.

    The click path is intentionally explicit: synchronous pointer move, window
    activation, then the button press. This avoids the case where Xvfb/Openbox
    leaves the browser unfocused while a synthetic event is sent. The pointer is
    read back afterwards for diagnostics.
    """
    if not _x_running():
        return {"ok": False, "error": "the X display is not running"}
    disp_w, disp_h = _display_geometry()
    if not (0 <= x < disp_w and 0 <= y < disp_h):
        return {
            "ok": False,
            "error": f"({x}, {y}) is outside the {disp_w}x{disp_h} display",
        }
    try:
        moved = _xdotool("mousemove", "--sync", str(x), str(y), timeout=10)
    except FileNotFoundError:
        return {"ok": False, "error": "xdotool is not installed on the remote computer"}
    except Exception as exc:
        return {"ok": False, "error": f"could not move pointer for click: {exc}"}
    if moved.returncode != 0:
        return {"ok": False, "error": (moved.stderr or "pointer move failed").strip()[:200]}

    _focus_window_under_cursor()

    try:
        pressed = _xdotool("click", "--clearmodifiers", "1", timeout=10)
    except FileNotFoundError:
        return {"ok": False, "error": "xdotool is not installed on the remote computer"}
    except Exception as exc:
        return {"ok": False, "error": f"could not click: {exc}"}
    if pressed.returncode != 0:
        return {"ok": False, "error": (pressed.stderr or "click failed").strip()[:200]}

    time.sleep(0.05)
    ax, ay = _pointer_position()
    landed = (ax == x and ay == y)
    _log(
        f"CLICK screen={disp_w}x{disp_h} model=({x},{y}) executed=({x},{y}) "
        f"actual=({ax},{ay}) {'LANDED' if landed else 'DRIFTED'}"
    )
    return {
        "ok": True,
        "x": x,
        "y": y,
        "actual_x": ax,
        "actual_y": ay,
        "landed": landed,
        "display_width": disp_w,
        "display_height": disp_h,
    }


#: The keys a model is allowed to press, mapped to the xdotool keysym.
#:
#: This table is the whole of the keyboard surface.  There is no code path from
#: a model-supplied string to an xdotool argument that is not a value here, and
#: xdotool is invoked with an argument list and no shell, so nothing in it can
#: become a command, a flag or a path.  A model that asks for a key that is not
#: listed gets a refusal, not a best guess.
_COMPUTER_KEYSYMS: dict[str, str] = {
    "ENTER": "Return",
    "RETURN": "Return",
    "TAB": "Tab",
    "ESC": "Escape",
    "ESCAPE": "Escape",
    "SPACE": "space",
    "BACKSPACE": "BackSpace",
    "DELETE": "Delete",
    "DEL": "Delete",
    "INSERT": "Insert",
    "HOME": "Home",
    "END": "End",
    "UP": "Up",
    "DOWN": "Down",
    "LEFT": "Left",
    "RIGHT": "Right",
    # The ARROW* spellings as well as the bare ones.  A model asked for an
    # arrow key reaches for ARROWDOWN, and a refusal there reads as "the keyboard
    # is broken" rather than "that is not a name I accept" -- both spellings are
    # the same xdotool keysym, so allowing both costs nothing.
    "ARROWUP": "Up",
    "ARROWDOWN": "Down",
    "ARROWLEFT": "Left",
    "ARROWRIGHT": "Right",
    "UPARROW": "Up",
    "DOWNARROW": "Down",
    "LEFTARROW": "Left",
    "RIGHTARROW": "Right",
    "PAGEUP": "Prior",
    "PAGEDOWN": "Next",
    "PRIOR": "Prior",
    "NEXT": "Next",
    "F1": "F1", "F2": "F2", "F3": "F3", "F4": "F4",
    "F5": "F5", "F6": "F6", "F7": "F7", "F8": "F8",
    "F9": "F9", "F10": "F10", "F11": "F11", "F12": "F12",
}

#: Modifiers, for combos such as CTRL+L.  Also an allowlist: "CTRL+X" is a
#: shortcut, but only because CTRL and X are both named here.
_COMPUTER_MODIFIERS: dict[str, str] = {
    "CTRL": "ctrl",
    "CONTROL": "ctrl",
    "ALT": "alt",
    "SHIFT": "shift",
    "META": "super",
    "SUPER": "super",
}

#: A single character key is allowed, because typing a URL into the address bar
#: is mostly letters.  One ASCII alphanumeric, and only ever as the final token.
_COMPUTER_SINGLE = set("abcdefghijklmnopqrstuvwxyz0123456789")

#: Longest string accepted in one type command.  Bounded so a model cannot use
#: the keyboard to write an unbounded amount into whatever has focus.
_COMPUTER_MAX_TEXT = 2000

#: Scroll notches are 3 lines each; 120px is a reasonable "one notch" and the
#: cap keeps a large delta from turning into hundreds of X round trips.
_COMPUTER_SCROLL_NOTCH = 120
_COMPUTER_SCROLL_MAX_STEPS = 25
_COMPUTER_MAX_SCROLL = 5000


def _computer_type(text: str) -> dict:
    """Type into whatever currently has focus on the REAL display.

    The text is a single argv element behind a `--` terminator, so a string that
    begins with a dash is typed rather than read as an option, and xdotool is
    run without a shell, so no part of it can be interpreted as a command.
    """
    if not _x_running():
        return {"ok": False, "error": "the X display is not running"}
    if not text:
        return {"ok": False, "error": "text is required"}
    if "\x00" in text:
        return {"ok": False, "error": "text cannot contain a null byte"}
    if len(text) > _COMPUTER_MAX_TEXT:
        return {
            "ok": False,
            "error": f"text is longer than {_COMPUTER_MAX_TEXT} characters; type it in parts",
        }
    try:
        # Focus first.  Key events go to whatever has input focus, and after a
        # click that is usually but not always the browser: a click on a
        # non-focusable part of the page, or the first action of a run before
        # anything has been clicked, leaves focus somewhere else and the text
        # goes to it.  This is the difference between "typing does nothing" and
        # typing working, and it is why the keystrokes are sent by the same
        # X client that the click was.
        _focus_window_under_cursor()
        r = _xdotool("type", "--clearmodifiers", "--delay", "12", "--", text, timeout=45)
    except FileNotFoundError:
        return {"ok": False, "error": "xdotool is not installed on the remote computer"}
    except Exception as exc:
        return {"ok": False, "error": f"could not type: {exc}"}
    if r.returncode != 0:
        return {"ok": False, "error": (r.stderr or "typing failed").strip()[:200]}
    return {"ok": True, "chars": len(text)}


def _computer_key(name: str) -> dict:
    """Press one allowlisted key or key combo on the REAL display.

    The xdotool argument is assembled entirely from the two tables above and
    joined with "+", so the model chooses from a fixed set of names and never
    supplies an argument.  "CTRL+L" and "ctrl+l" are the same request; anything
    not in the tables is refused with the list of what is allowed.
    """
    if not _x_running():
        return {"ok": False, "error": "the X display is not running"}
    raw = (name or "").strip()
    if not raw:
        return {"ok": False, "error": "key is required"}
    parts = [p for p in raw.replace("-", "+").split("+") if p.strip()]
    if not parts:
        return {"ok": False, "error": "key is required"}
    parts = [p.strip().upper() for p in parts]
    if len(parts) > 3:
        return {"ok": False, "error": "a key combo may have at most two modifiers and one key"}

    base = parts[-1]
    mods = parts[:-1]
    keysym: str
    if len(base) == 1:
        # A literal character, for shortcuts like CTRL+L and CTRL+A.  The token
        # was upper-cased with the rest of the request, so it is matched and
        # emitted in lower case: a shift is implied by the letter itself.
        if base.lower() not in _COMPUTER_SINGLE:
            return {"ok": False, "error": f"{base!r} is not an allowed key"}
        keysym = base.lower()
    elif base in _COMPUTER_KEYSYMS:
        keysym = _COMPUTER_KEYSYMS[base]
    else:
        allowed = ", ".join(sorted(_COMPUTER_KEYSYMS))
        return {
            "ok": False,
            "error": f"{base!r} is not an allowed key. Allowed: {allowed}, "
            "or CTRL/ALT/SHIFT/META plus one of those, or a single letter or digit",
        }

    resolved: list[str] = []
    seen: set[str] = set()
    for mod in mods:
        if mod not in _COMPUTER_MODIFIERS:
            allowed_mods = ", ".join(sorted(set(_COMPUTER_MODIFIERS)))
            return {"ok": False, "error": f"{mod!r} is not an allowed modifier. Allowed: {allowed_mods}"}
        token = _COMPUTER_MODIFIERS[mod]
        if token not in seen:
            seen.add(token)
            resolved.append(token)
    combo = "+".join([*resolved, keysym])

    try:
        # Same reason as typing: the keys go to the focused window, and it is
        # not always the browser.
        _focus_window_under_cursor()
        r = _xdotool("key", "--clearmodifiers", combo)
    except FileNotFoundError:
        return {"ok": False, "error": "xdotool is not installed on the remote computer"}
    except Exception as exc:
        return {"ok": False, "error": f"could not press {combo}: {exc}"}
    if r.returncode != 0:
        return {"ok": False, "error": (r.stderr or "key press failed").strip()[:200]}
    return {"ok": True, "key": combo}


def _computer_scroll(delta_y: int) -> dict:
    """Scroll the REAL focused window with the real wheel buttons.

    xdotool has no scroll verb, so this presses button 4 (up) or 5 (down), which
    is what a physical wheel does.  A large delta is split into notches instead
    of one enormous jump, because a browser that gets a 600px wheel in a single
    event scrolls differently than one that gets five 120px events.
    """
    if not _x_running():
        return {"ok": False, "error": "the X display is not running"}
    if delta_y == 0:
        return {"ok": False, "error": "delta_y must not be zero; use a positive or negative value"}
    if abs(delta_y) > _COMPUTER_MAX_SCROLL:
        return {
            "ok": False,
            "error": f"delta_y must be between -{_COMPUTER_MAX_SCROLL} and {_COMPUTER_MAX_SCROLL}",
        }
    button = "5" if delta_y > 0 else "4"  # 5 is down, 4 is up
    steps = max(1, min(abs(delta_y) // _COMPUTER_SCROLL_NOTCH, _COMPUTER_SCROLL_MAX_STEPS))
    try:
        # The button name is chosen above, never taken from the request.
        r = _xdotool("click", "--repeat", str(steps), "--delay", "40", button, timeout=30)
    except FileNotFoundError:
        return {"ok": False, "error": "xdotool is not installed on the remote computer"}
    except Exception as exc:
        return {"ok": False, "error": f"could not scroll: {exc}"}
    if r.returncode != 0:
        return {"ok": False, "error": (r.stderr or "scroll failed").strip()[:200]}
    return {"ok": True, "delta_y": delta_y, "steps": steps}


def _computer_move(x: int, y: int) -> dict:
    """Move the REAL pointer without clicking."""
    if not _x_running():
        return {"ok": False, "error": "the X display is not running"}
    disp_w, disp_h = _display_geometry()
    if not (0 <= x < disp_w and 0 <= y < disp_h):
        return {
            "ok": False,
            "error": f"({x}, {y}) is outside the {disp_w}x{disp_h} display",
        }
    try:
        r = _xdotool("mousemove", "--sync", str(x), str(y))
    except FileNotFoundError:
        return {"ok": False, "error": "xdotool is not installed on the remote computer"}
    except Exception as exc:
        return {"ok": False, "error": f"could not move the pointer: {exc}"}
    if r.returncode != 0:
        return {"ok": False, "error": (r.stderr or "mousemove failed").strip()[:200]}
    ax, ay = _pointer_position()
    _log(
        f"MOVE screen={disp_w}x{disp_h} model=({x},{y}) executed=({x},{y}) "
        f"actual=({ax},{ay})"
    )
    return {
        "ok": True,
        "x": x,
        "y": y,
        "actual_x": ax,
        "actual_y": ay,
        "landed": (ax == x and ay == y),
        "display_width": disp_w,
        "display_height": disp_h,
    }


def _safe_navigate(url: str) -> dict:
    """Point the real Chrome at a URL.

    Only http and https reach the browser.  A model that returned
    `file:///...` or `javascript:` is not navigating, it is trying to read the
    user's disk or run code in the page, and neither is a thing this loop does.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return {"ok": False, "error": f"refusing to navigate to {parsed.scheme or 'no'} scheme"}
    wsurl = _cdp_target_wsurl()
    if not wsurl:
        return {"ok": False, "error": "chrome is not running on the debug port"}
    try:
        import websocket

        ws = _cdp_connect(wsurl)
        try:
            _cdp_send(ws, 1, "Page.navigate", {"url": url})
            for _ in range(40):
                raw = ws.recv()
                if not raw:
                    continue
                msg = json.loads(raw)
                if msg.get("id") == 1:
                    result = msg.get("result") or {}
                    if result.get("errorText"):
                        return {"ok": False, "error": str(result["errorText"])}
                    return {"ok": True, "url": url}
        finally:
            try:
                ws.close()
            except Exception:
                pass
    except Exception as exc:
        return {"ok": False, "error": f"navigation failed: {exc}"}
    return {"ok": False, "error": "navigation produced no response"}


def _computer_search(query: str) -> dict:
    """Search, in the remote browser, by navigating it to the search engine.

    This is a navigation on purpose.  Typing into the search box would need a
    click whose target the model has not been shown yet -- on the first turn it
    has seen no screenshot at all, so there is no coordinate to aim at.  Going
    straight to the engine's query URL is the same search the user would get, and
    it is reachable from turn one.
    """
    clean = (query or "").strip()
    if not clean:
        return {"ok": False, "error": "empty search query"}
    return _safe_navigate(_SEARCH_ENGINE + quote(clean, safe=""))


def sysinfo() -> dict:
    def _mem():
        try:
            with open("/proc/meminfo") as f:
                lines = f.read().splitlines()
            mem = {}
            for ln in lines:
                k, _, v = ln.partition(":")
                mem[k] = int(v.strip().split()[0])
            total = mem.get("MemTotal", 0)
            avail = mem.get("MemAvailable", mem.get("MemFree", 0))
            return {"total_kb": total, "used_kb": total - avail}
        except Exception:
            return {"total_kb": 0, "used_kb": 0}

    def _disk():
        import shutil
        try:
            usage = shutil.disk_usage(WORKSPACE)
            return {"total": usage.total, "used": usage.used, "free": usage.free}
        except Exception:
            return {"total": 0, "used": 0, "free": 0}

    def _cpu():
        try:
            with open("/proc/stat") as f:
                fields = f.readline().split()[1:]
            nums = list(map(int, fields))
            idle = nums[3] + (nums[4] if len(nums) > 4 else 0)
            total = sum(nums)
            return {"idle": idle, "total": total, "cores": os.cpu_count() or 1}
        except Exception:
            return {"idle": 0, "total": 0, "cores": 1}

    def _uptime():
        try:
            with open("/proc/uptime") as f:
                return float(f.read().split()[0])
        except Exception:
            return 0.0

    # The Settings app shows these, so they are part of the same read-only
    # payload rather than a new endpoint: one request, one auth check, and no
    # extra surface for the browser to talk to.

    def _os_release() -> str:
        try:
            with open("/etc/os-release") as f:
                for ln in f:
                    if ln.startswith("PRETTY_NAME="):
                        name = ln.partition("=")[2].strip().strip('"')
                        if name:
                            return name
        except Exception:
            pass
        return os.uname().sysname if hasattr(os, "uname") else "unknown"


    def _browser() -> dict:
        """Is the real desktop Chrome up?  Port + recorded pid, nothing else.

        Deliberately reports state rather than acting on it: no restart, no
        navigate, no shell.  The Settings app is a read-only viewer.
        """
        listening = _port_listening(CHROME_DEBUG_PORT)
        pid = _read_pid("chromium") or _read_pid("google-chrome")
        return {
            "running": bool(listening or _pid_alive(pid)),
            "cdp": listening,
            "pid": pid if _pid_alive(pid) else None,
            "start_url": START_URL,
            "display": DESKTOP_DISPLAY,
        }

    def _display() -> dict:
        return {
            "width": _desktop_width(),
            "height": _desktop_height(),
            "size": DESKTOP_SIZE,
        }

    def _load() -> list:
        try:
            one, five, fifteen = os.getloadavg()
            return [one, five, fifteen]
        except Exception:
            return [0.0, 0.0, 0.0]

    return {
        "mem": _mem(),
        "disk": _disk(),
        "cpu": _cpu(),
        "uptime": _uptime(),
        "hostname": os.uname().nodename if hasattr(os, "uname") else WORKSPACE,
        "os": _os_release(),
        "kernel": os.uname().release if hasattr(os, "uname") else "",
        "browser": _browser(),
        "display": _display(),
        "loadavg": _load(),
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def log_message(self, fmt, *args):
        pass

    def _json(self, code: int, payload: dict):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _sse_serve(self, q, subs_list, subs_lock):
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self._cors()
            self.end_headers()
            try:
                with subs_lock:
                    subs_list.append(q)
            except RuntimeError:
                raise
            latest = _desktop_latest_b64
            if latest:
                try:
                    q.put_nowait(latest)
                except Exception:
                    pass
            try:
                while True:
                    try:
                        b64 = q.get(timeout=15)
                    except Exception:
                        continue
                    payload = f"data: {b64}\n\n".encode()
                    try:
                        self.wfile.write(f"{len(payload):x}\r\n".encode())
                        self.wfile.write(payload)
                        self.wfile.write(b"\r\n")
                        self.wfile.flush()
                    except Exception:
                        break
            finally:
                with subs_lock:
                    try:
                        subs_list.remove(q)
                    except ValueError:
                        pass
        except Exception as exc:
            try:
                self._json(500, {"ok": False, "error": str(exc)})
            except Exception:
                pass

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        if path == "/status":
            self._json(200, {"ok": True, "workspace": WORKSPACE, "pid": os.getpid()})
        elif path == "/sysinfo":
            self._json(200, sysinfo())
        elif path == "/screen":
            if os.path.exists(SCREEN_FILE):
                with open(SCREEN_FILE, "rb") as f:
                    data = f.read()
                if data[:3] == b"\xff\xd8\xff":
                    mime = "image/jpeg"
                else:
                    mime = "image/png"
                self.send_response(200)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(len(data)))
                self._cors()
                self.end_headers()
                self.wfile.write(data)
            else:
                self._json(404, {"ok": False, "error": "no screenshot yet"})
        elif path == "/cdp/status":
            wsurl = _cdp_target_wsurl()
            if not wsurl:
                self._json(503, {"ok": False, "error": "chrome not running on debug port", "url": "", "title": ""})
                return
            url, title = "", ""
            try:
                import websocket
                ws = _cdp_connect(wsurl)
                try:
                    _cdp_send(ws, 1, "Runtime.evaluate",
                              {"expression": "JSON.stringify({u:location.href,t:document.title})",
                               "returnByValue": True})
                    for _ in range(50):
                        raw = ws.recv()
                        if not raw:
                            continue
                        msg = json.loads(raw)
                        if msg.get("id") == 1:
                            val = (msg.get("result") or {}).get("result", {}).get("value", "{}")
                            try:
                                info = json.loads(val)
                            except Exception:
                                info = {}
                            url = info.get("u", "")
                            title = info.get("t", "")
                            break
                finally:
                    try:
                        ws.close()
                    except Exception:
                        pass
                self._json(200, {"ok": True, "url": url, "title": title})
            except Exception as exc:
                self._json(200, {"ok": True, "url": url, "title": title, "warn": str(exc)})
        elif path == "/cdp/stream":
            try:
                _cdp_stream_start()
                import queue
                self._sse_serve(queue.Queue(maxsize=1), _stream_queues, _cdp_subs)
            except Exception as exc:
                try:
                    self._json(500, {"ok": False, "error": str(exc)})
                except Exception:
                    pass
        elif path == "/desktop/stream":
            try:
                _desktop_stream_start()
                import queue
                self._sse_serve(queue.Queue(maxsize=1), _desktop_subs, _desktop_subs_lock)
            except Exception as exc:
                try:
                    self._json(500, {"ok": False, "error": str(exc)})
                except Exception:
                    pass
        elif path == "/desktop/status":
            try:
                x = os.path.isdir("/tmp/.X11-unix")
                self._json(200, {
                    "display": DESKTOP_DISPLAY,
                    "x_socket": x,
                    "subscribers": len(_desktop_subs),
                    "alive": _desktop_broadcast_t is not None and _desktop_broadcast_t.is_alive(),
                })
            except Exception as exc:
                self._json(500, {"ok": False, "error": str(exc)})
        elif path == "/display/status":
            try:
                self._json(200, {"ok": True, **_desktop_status()})
            except Exception as exc:
                self._json(500, {"ok": False, "error": str(exc)})
        elif path == "/display/ensure":
            try:
                self._json(200, {"ok": True, **_desktop_ensure()})
            except Exception as exc:
                self._json(500, {"ok": False, "error": str(exc)})
        elif path == "/file":
            target = safe_path(WORKSPACE, query.get("path", [""])[0])
            if not os.path.isfile(target):
                self._json(404, {"ok": False, "error": f"file not found: {target}"})
                return
            mime = mimetypes.guess_type(target)[0] or "application/octet-stream"
            with open(target, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self._cors()
            self.end_headers()
            self.wfile.write(data)
        else:
            self._json(404, {"ok": False, "error": "not found"})

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_POST(self):
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        try:
            req = json.loads(raw.decode() or "{}")
        except Exception:
            self._json(400, {"ok": False, "error": "bad json"})
            return
        if path == "/cdp/input":
            _enqueue_cdp_input(req)
            self._json(202, {"ok": True, "queued": True})
            return
        # --- computer control -------------------------------------------------
        # Driven by the AI loop in backend/app/computer.  Every one of these acts
        # on the local X display or the local Chrome and nothing else.  The agent
        # binds loopback only and is not forwarded, so these add routes to an
        # already-reachable service, not a new door.  Anything a model can name
        # is re-validated here as well as in the app: the loopback bind is the
        # trust boundary, and these are the second lock on the same door.
        if path == "/computer/screen":
            self._json(200, _capture_display(bool(req.get("draw_mouse", True))))
            return
        if path == "/computer/click":
            try:
                x = int(req.get("x"))
                y = int(req.get("y"))
            except (TypeError, ValueError):
                self._json(400, {"ok": False, "error": "x and y must be integers"})
                return
            width, height = _display_geometry()
            if not (0 <= x < width and 0 <= y < height):
                self._json(400, {"ok": False, "error": f"outside the {width}x{height} display"})
                return
            self._json(200, _computer_click(x, y))
            return
        if path == "/computer/navigate":
            url = str(req.get("url") or "").strip()
            if not url:
                self._json(400, {"ok": False, "error": "url is required"})
                return
            self._json(200, _safe_navigate(url))
            return
        if path == "/computer/type":
            text = req.get("text")
            if not isinstance(text, str):
                self._json(400, {"ok": False, "error": "text must be a string"})
                return
            self._json(200, _computer_type(text))
            return
        if path == "/computer/key":
            name = req.get("key")
            if not isinstance(name, str):
                self._json(400, {"ok": False, "error": "key must be a string"})
                return
            self._json(200, _computer_key(name))
            return
        if path == "/computer/scroll":
            raw = req.get("delta_y")
            if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                self._json(400, {"ok": False, "error": "delta_y must be a number"})
                return
            import math as _math

            if _math.isnan(float(raw)) or _math.isinf(float(raw)):
                self._json(400, {"ok": False, "error": "delta_y must be a finite number"})
                return
            self._json(200, _computer_scroll(int(raw)))
            return
        if path == "/computer/move":
            try:
                x = int(req.get("x"))
                y = int(req.get("y"))
            except (TypeError, ValueError):
                self._json(400, {"ok": False, "error": "x and y must be integers"})
                return
            width, height = _desktop_width(), _desktop_height()
            if not (0 <= x < width and 0 <= y < height):
                self._json(400, {"ok": False, "error": f"outside the {width}x{height} display"})
                return
            self._json(200, _computer_move(x, y))
            return
        if path == "/computer/search":
            self._json(200, _computer_search(str(req.get("query") or "")))
            return
        if path == "/computer/state":
            url, title = "", ""
            wsurl = _cdp_target_wsurl()
            if wsurl:
                try:
                    import websocket

                    ws = _cdp_connect(wsurl)
                    try:
                        _cdp_send(ws, 1, "Runtime.evaluate",
                                  {"expression": "JSON.stringify({u:location.href,t:document.title})",
                                   "returnByValue": True})
                        for _ in range(30):
                            raw = ws.recv()
                            if not raw:
                                continue
                            msg = json.loads(raw)
                            if msg.get("id") == 1:
                                val = (msg.get("result") or {}).get("result", {}).get("value", "{}")
                                try:
                                    info = json.loads(val)
                                except Exception:
                                    info = {}
                                url, title = info.get("u", ""), info.get("t", "")
                                break
                    finally:
                        try:
                            ws.close()
                        except Exception:
                            pass
                except Exception:
                    pass
            self._json(200, {
                "ok": True,
                "url": url,
                "title": title,
                "width": _desktop_width(),
                "height": _desktop_height(),
            })
            return
        if path in ("/display/restart", "/display/chromium/restart", "/display/ensure"):
            try:
                chromium_only = path == "/display/chromium/restart"
                result = _desktop_restart(chromium_only=chromium_only)
                self._json(200, {"ok": True, "restarted": path, **result})
            except Exception as exc:
                self._json(500, {"ok": False, "error": str(exc)})
            return
        if path == "/display/stop":
            try:
                result = _desktop_stop()
                self._json(200, {"ok": True, "stopped": True, **result})
            except Exception as exc:
                self._json(500, {"ok": False, "error": str(exc)})
            return
        if path != "/tool/exec":
            self._json(404, {"ok": False, "error": "not found"})
            return
        tool = req.get("tool", "")
        args = req.get("args", {})
        timeout = int(req.get("timeout", 120))
        result = exec_tool(tool, args, timeout)
        result["ok"] = result.get("ok", "error" not in result)
        self._json(200, result)


def main(port: int = 9000, host: str = "127.0.0.1"):
    # Bring up the real graphical session immediately so the VM screen is
    # already there by the time the backend probes it.
    try:
        _desktop_ensure()
    except Exception:
        pass
    threading.Thread(
        target=_desktop_supervisor_loop, name="desktop-supervisor", daemon=True
    ).start()
    # 127.0.0.1, never 0.0.0.0.  This daemon can drive the browser, read the
    # user's files and run shell commands on their behalf, so it is the single
    # most damaging port on the machine to expose.  Nothing reaches it from
    # outside: the backend talks to it over loopback, and the Quick Tunnel only
    # publishes the app on :8000.
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"vm-agent daemon listening on {host}:{port} workspace={WORKSPACE}", flush=True)
    print(
        f"desktop display={DESKTOP_DISPLAY} size={DESKTOP_SIZE} vnc_port={VNC_PORT}",
        flush=True,
    )
    server.serve_forever()


if __name__ == "__main__":
    import sys

    # --host is opt-in and only for someone deliberately debugging on another
    # host; the default is loopback because that is the only correct value in a
    # Codespace.  Refuse 0.0.0.0 unless it is asked for by name.
    _args = sys.argv[1:]
    _host = os.environ.get("AGENT_HOST", "127.0.0.1")
    if "--host" in _args:
        _i = _args.index("--host")
        if _i + 1 < len(_args):
            _host = _args[_i + 1]
            del _args[_i : _i + 2]
    main(int(_args[0]) if _args else 9000, _host)
