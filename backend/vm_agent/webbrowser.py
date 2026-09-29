import base64
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time

WORKSPACE = os.environ.get("WORKSPACE") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCREEN = os.path.join(WORKSPACE, ".screen.png")
DEBUG_PORT = int(os.environ.get("CHROME_DEBUG_PORT", "9222"))
AGENT_PORT = int(os.environ.get("AGENT_PORT", "9000"))
# Must match the daemon's CHROME_PROFILE: this tool attaches to the browser the
# daemon supervises, and only launches its own when that one is missing.  Two
# different profile paths would mean two browsers and two different sessions.
PROFILE_DIR = os.environ.get("CHROME_PROFILE", "/workspaces/chrome-profile")
DISPLAY = os.environ.get("DESKTOP_DISPLAY", ":99")
MAX_OUTPUT = 8000
MAX_IMAGE_CHARS = 400_000

_VM_DEBUG = os.environ.get("RAGAGENT_DEBUG") == "1"


def log(*a):
    if _VM_DEBUG:
        print(*a, file=sys.stderr, flush=True)


def find_chromium() -> str:
    """Google Chrome first; the Chromium packages are only a fallback."""
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
    return shutil.which("google-chrome") or "/usr/bin/chromium"


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.4):
            return True
    except Exception:
        return False


def _ask_agent(path: str, timeout: float = 4.0):
    """Best-effort call into the vm-agent daemon running in this guest."""
    import urllib.request
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{AGENT_PORT}{path}")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "ignore"))
    except Exception:
        return None


def _ensure_desktop_browser(timeout: float = 45.0) -> bool:
    """Make sure the browser serving CDP is the headful window on the display.

    The daemon supervises Chromium, Xvfb, the window manager and x11vnc.  Asking
    it to ensure the stack keeps a single browser process, so the window visible
    in the VM screen is exactly the browser this tool automates.  Returns False
    when the agent cannot be reached, so the caller can fall back to launching
    Chromium itself.
    """
    info = _ask_agent("/display/ensure", timeout=timeout)
    if not info or not info.get("ok"):
        return False
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _port_open(DEBUG_PORT):
            return True
        time.sleep(0.5)
    return _port_open(DEBUG_PORT)


def _clear_profile_locks() -> None:
    try:
        os.makedirs(PROFILE_DIR, exist_ok=True)
    except Exception:
        return
    for suffix in ("SingletonLock", "SingletonSocket", "SingletonCookie", "Singleton.*"):
        try:
            for name in os.listdir(PROFILE_DIR):
                if name.startswith("Singleton"):
                    try:
                        os.remove(os.path.join(PROFILE_DIR, name))
                    except Exception:
                        pass
        except Exception:
            return


def _launch_background_chrome():
    """Launch headful Chromium on the real X display (never --headless).

    A headless browser renders off-screen, so it cannot appear in the VNC
    framebuffer; keeping the window real is what makes the Computer screen an
    honest view of the VM.
    """
    log("launching headful chrome on", DISPLAY, "port", DEBUG_PORT)
    _clear_profile_locks()
    env = dict(os.environ)
    env["DISPLAY"] = DISPLAY
    args = [
        find_chromium(),
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-networking",
        "--disable-sync",
        "--disable-features=Translate,MediaRouter,OptimizationHints",
        "--remote-allow-origins=*",
        # No --kiosk: it hides the tab strip, new-tab button and address bar.
        "--start-maximized",
        "--window-size=1280,800",
        "--window-position=0,0",
        f"--remote-debugging-port={DEBUG_PORT}",
        f"--user-data-dir={PROFILE_DIR}",
        "about:blank",
    ]
    subprocess.Popen(
        args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
        start_new_session=True,
    )
    for _ in range(90):
        if _port_open(DEBUG_PORT):
            return
        time.sleep(0.5)
    raise RuntimeError("chrome failed to start")


def _attach():
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service

    opts = Options()
    opts.add_experimental_option("debuggerAddress", f"127.0.0.1:{DEBUG_PORT}")
    opts.binary_location = find_chromium()
    bin_path = "/usr/bin/chromedriver" if os.path.exists("/usr/bin/chromedriver") else "chromedriver"
    driver = webdriver.Chrome(service=Service(bin_path), options=opts)
    return driver


def _detach(driver):
    try:
        driver.stop_client()
    except Exception:
        pass
    try:
        svc = getattr(driver, "service", None)
        if svc and svc.process and svc.process.poll() is None:
            svc.process.terminate()
            try:
                svc.process.wait(timeout=3)
            except Exception:
                svc.process.kill()
    except Exception:
        pass


def _scroll_into_view(sel, el):
    try:
        sel.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
    except Exception:
        pass


def _element_at(sel, x, y):
    el = sel.execute_script("return document.elementFromPoint(arguments[0], arguments[1]);", int(x), int(y))
    if el is None:
        return sel.find_element("tag name", "body")
    return el


def _click_at(sel, el, x, y):
    try:
        try:
            el.click()
        except Exception:
            from selenium.webdriver.common.action_chains import ActionChains
            rect = sel.execute_script(
                "var r=arguments[0].getBoundingClientRect(); return [r.left, r.top];", el
            )
            ActionChains(sel).move_to_element_with_offset(el, int(x) - rect[0], int(y) - rect[1]).click().perform()
    except Exception:
        sel.execute_script("arguments[0].click();", el)
    try:
        sel.execute_script(
            "var e=document.activeElement; if(e && e.focus){ e.focus(); if(e.tagName==='INPUT'||e.tagName==='TEXTAREA'||e.isContentEditable){ var t=e.value||e.textContent||''; try{e.selectionStart=e.selectionEnd=t.length;}catch(_){}} }",
        )
    except Exception:
        pass
    time.sleep(0.4)


def _sync_viewport(sel):
    """Match the CDP capture surface to the real on-screen window.

    Chromium is headful now, so its content area is the window minus whatever
    the window manager reserves.  Forcing a fixed 1280x800 here would make
    agent screenshots disagree with what the VNC screen actually shows, so the
    override is set from the window's own inner size instead.
    """
    try:
        size = sel.execute_script("return [window.innerWidth, window.innerHeight]")
        w, h = int(size[0]), int(size[1])
    except Exception:
        return
    if w < 200 or h < 200:
        return
    try:
        sel.execute_cdp_cmd(
            "Emulation.setDeviceMetricsOverride",
            {
                "width": w,
                "height": h,
                "deviceScaleFactor": 0,
                "mobile": False,
                "screenWidth": w,
                "screenHeight": h,
            },
        )
    except Exception:
        pass


def _snapshot(sel, url):
    os.makedirs(WORKSPACE, exist_ok=True)
    fmt = "png"
    try:
        res = sel.execute_cdp_cmd("Page.captureScreenshot", {"format": "jpeg", "quality": 70})
        b64 = res.get("data", "")
        fmt = "jpeg" if b64 else "png"
    except Exception:
        b64 = ""
    if not b64 or fmt != "jpeg":
        try:
            b64 = base64.b64encode(sel.get_screenshot_as_png()).decode()
            fmt = "png"
        except Exception:
            b64 = ""
    if b64:
        try:
            with open(SCREEN, "wb") as f:
                f.write(base64.b64decode(b64))
        except Exception:
            pass
    if len(b64) > MAX_IMAGE_CHARS:
        b64 = "[truncated; full is %d chars] " % len(b64) + b64[:MAX_IMAGE_CHARS]
    try:
        title = sel.title
    except Exception:
        title = ""
    try:
        cur = sel.current_url
    except Exception:
        cur = ""
    return {"path": SCREEN, "image": b64, "format": fmt, "title": title, "url": cur}


def main():
    req = json.loads(sys.argv[1])
    action = str(req.get("action", "goto")).lower()
    url = str(req.get("url", "")).strip()
    timeout = int(req.get("timeout", 45))
    sel = None
    try:
        for _ in range(2):
            if _port_open(DEBUG_PORT):
                break
            if not _ensure_desktop_browser():
                _launch_background_chrome()
        sel = _attach()
        sel.set_page_load_timeout(min(timeout, 60))
        _sync_viewport(sel)
        out = ""
        if action == "goto":
            if not url:
                return {"ok": False, "error": "goto requires url"}
            try:
                sel.get(url)
            except Exception:
                pass
            time.sleep(0.3)
            out = sel.find_element("tag name", "body").text[:MAX_OUTPUT]
        elif action == "text":
            out = sel.find_element("tag name", "body").text[:MAX_OUTPUT]
        elif action == "click":
            sel_x = str(req.get("selector", "") or "").strip()
            if sel_x:
                el = sel.find_element("css selector", sel_x)
                _scroll_into_view(sel, el)
                el.click()
                time.sleep(0.3)
            elif ("x" in req) and ("y" in req):
                x, y = int(req["x"]), int(req["y"])
                _click_at(sel, _element_at(sel, x, y), x, y)
            else:
                return {"ok": False, "error": "click requires selector or x/y"}
        elif action == "type":
            sel_x = str(req.get("selector", "") or "").strip()
            text = str(req.get("text", ""))
            need_enter = bool(str(req.get("enter", "") or "").lower() in ("1", "true", "yes"))
            if sel_x:
                el = sel.find_element("css selector", sel_x)
                _scroll_into_view(sel, el)
                try:
                    el.clear()
                except Exception:
                    pass
            elif ("x" in req) and ("y" in req):
                x, y = int(req["x"]), int(req["y"])
                _click_at(sel, _element_at(sel, x, y), x, y)
                el = sel.switch_to.active_element
            else:
                el = sel.switch_to.active_element
            el.send_keys(text)
            if need_enter:
                from selenium.webdriver.common.keys import Keys
                el.send_keys(Keys.ENTER)
            time.sleep(0.2)
        elif action in ("press", "enter"):
            from selenium.webdriver.common.keys import Keys
            key = str(req.get("key", "Enter")).replace(" ", "").upper()
            sel_x = str(req.get("selector", "") or "").strip()
            if sel_x:
                el = sel.find_element("css selector", sel_x)
                el.send_keys(getattr(Keys, key, Keys.ENTER))
            else:
                sel.switch_to.active_element.send_keys(getattr(Keys, key, Keys.ENTER))
            time.sleep(0.3)
        elif action == "scroll":
            dx = int(str(req.get("dx", "0")).strip() or 0)
            dy = int(str(req.get("dy", "0")).strip() or 0)
            sel.execute_script("window.scrollBy(arguments[0], arguments[1]);", dx, dy)
            time.sleep(0.2)
        elif action == "back":
            try:
                sel.back()
            except Exception:
                pass
            time.sleep(0.3)
        elif action in ("forward", "fwd"):
            try:
                sel.forward()
            except Exception:
                pass
            time.sleep(0.3)
        elif action == "eval":
            script = str(req.get("script", "return location.href;"))
            return {"ok": True, "output": str(sel.execute_script(script))}
        else:
            return {"ok": False, "error": f"unknown action {action}"}
        snap = _snapshot(sel, url)
        return {
            "ok": True,
            "output": (f"visited {url} - {snap['title']}\n\n{out}" if action == "goto" else out or snap["title"]),
            "title": snap["title"],
            "url": snap["url"],
            "path": snap["path"],
            "image": snap["image"],
            "format": snap.get("format", "png"),
        }
    except Exception as exc:
        return {"ok": False, "error": f"browser {action}: {exc}"}
    finally:
        if sel is not None:
            _detach(sel)


if __name__ == "__main__":
    print(json.dumps(main()))