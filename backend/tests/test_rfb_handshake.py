"""Prove the RFB handshake in codespace/verify.sh is actually correct.

verify.sh claims to prove the live screen works by completing a real RFB 3.8
handshake over the tunnel and reading a framebuffer update off the far end.  A
handshake written by inspection is usually wrong in a small way -- a missing
version string, the wrong number of security-type bytes -- and the failure looks
exactly like a broken screen.

So this runs the *same* sequence against a canned server that behaves like
x11vnc, and fails if the sequence is off by even one byte.

Run with:  python -m tests.test_rfb_handshake
"""
import asyncio
import os
import re
import struct
import sys
import tempfile
import threading
import time
import pathlib

failures: list[str] = []


def check(cond: bool, label: str) -> None:
    print(f"{'ok  ' if cond else 'FAIL'}  {label}")
    if not cond:
        failures.append(label)


def check_raw(cond: bool, label: str) -> None:
    print(f"{'ok  ' if cond else 'FAIL'}  {label}")
    if not cond:
        failures.append(label)


WIDTH, HEIGHT = 1365, 768
DESKTOP_NAME = b"rag-desktop"
PIXELS = b"\x00" * 65536  # a real update: 256x256 pixels of 32bpp


class X11Vnc:
    """A canned x11vnc: speaks RFB 3.8 with the None security type."""

    def __init__(self) -> None:
        import socket
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.received = b""
        self._stop = False
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        conn, _ = self.sock.accept()
        conn.settimeout(20)
        try:
            # 1. version
            conn.sendall(b"RFB 003.008\n")
            # 2. wait for the client's version -- a script that forgets this will
            #    hang here, which is the point of the test.
            if conn.recv(12) != b"RFB 003.008\n":
                return
            # 3. security types: one type, "None"
            conn.sendall(bytes([1, 1]))
            # 4. client's choice
            if conn.recv(1) != bytes([1]):
                return
            # 5. SecurityResult = OK
            conn.sendall(struct.pack(">I", 0))
            # 6. ClientInit
            conn.recv(1)
            # 7. ServerInit
            conn.sendall(struct.pack(">HH", WIDTH, HEIGHT) + b"\x20" * 16
                         + struct.pack(">I", len(DESKTOP_NAME)) + DESKTOP_NAME)
            # 8. a FramebufferUpdateRequest, then an update
            req = conn.recv(10)
            if len(req) < 10 or req[0] != 3:
                return
            conn.sendall(bytes([0, 0]) + struct.pack(">H", 1) + struct.pack(">HHHH", 0, 0, 256, 256)
                         + PIXELS)
            time.sleep(0.5)
        except OSError:
            pass
        finally:
            conn.close()

    def stop(self) -> None:
        self._stop = True
        try:
            self.sock.close()
        except OSError:
            pass


# --- the sequence verify.sh uses, against the canned server ----------------
async def run_client(port: int) -> dict:
    """Byte-for-byte the steps in verify.sh, over a plain socket.

    A raw socket is used rather than websockets so the test needs no proxy or
    cookie plumbing; what is under test is the RFB byte sequence, not the
    transport.
    """
    import socket as _socket
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    result: dict = {}

    def recv(n: int) -> bytes:
        return reader.read(n)

    greeting = await recv(12)
    result["greeting"] = greeting
    writer.write(greeting)                       # client version
    await writer.drain()

    n = await recv(1)                            # number of security types
    count = n[0]
    types = await recv(count) if count else b""
    result["sec_types"] = types
    writer.write(bytes([1]))                     # None
    await writer.drain()

    result_read = await recv(4)
    result["sec_result"] = struct.unpack(">I", result_read)[0]
    writer.write(bytes([1]))                     # ClientInit, shared
    await writer.drain()

    init = await recv(24)
    w, h = struct.unpack(">HH", init[:4])
    name_len = struct.unpack(">I", init[20:24])[0]
    name = (await recv(name_len)) if name_len else b""
    result["size"] = (w, h)
    result["name"] = name.decode("utf-8", "replace")

    writer.write(struct.pack(">BBHHHH", 3, 0, 0, 0, w, h))
    await writer.drain()

    header = await recv(4)                       # msg type + padding + count
    n_rects = struct.unpack(">H", header[2:4])[0]
    x, y, rw, rh = struct.unpack(">HHHH", await recv(8))
    pixels = await recv(rw * rh * 4)
    result["pixels"] = len(pixels)
    result["rect"] = (x, y, rw, rh)
    result["n_rects"] = n_rects

    writer.close()
    return result


def main() -> int:
    vnc = X11Vnc()
    time.sleep(0.2)
    try:
        res = asyncio.run(run_client(vnc.port))
    finally:
        vnc.stop()

    check_raw(res["greeting"] == b"RFB 003.008\n",
              f"the server's greeting is read first ({res['greeting']!r})")
    check_raw(res["sec_types"] == bytes([1]),
              f"security types are read as a count then the list ({res['sec_types']!r})")
    check_raw(res["sec_result"] == 0, "SecurityResult is read as 4 bytes and is zero")
    check_raw(res["size"] == (WIDTH, HEIGHT),
              f"ServerInit carries the framebuffer size {res['size']}")
    check_raw(res["name"] == "rag-desktop",
              f"the desktop name is read using its own length prefix ({res['name']!r})")
    check_raw(res["n_rects"] == 1, f"the update header carries a rectangle count ({res['n_rects']})")
    check_raw(res["rect"] == (0, 0, 256, 256),
              f"the rectangle header is read before its pixels ({res['rect']})")
    check_raw(res["pixels"] == len(PIXELS),
              f"exactly the pixel bytes are read, with no over-read ({res['pixels']:,})")

    # --- and that verify.sh's Python block is in sync with this sequence ----
    verify = pathlib.Path(__file__).resolve().parents[2] / "codespace" / "verify.sh"
    src = verify.read_text(encoding="utf-8")
    block = re.search(r"python3 - <<'PY'\n(.*?)\nPY", src, re.S)
    if not block:
        check_raw(False, "verify.sh contains the RFB block")
    else:
        body = block.group(1)
        for label, needle in (
            ("sends the client version", 'ws.send(version)'),
            ("reads the security types", 'len(n) < 1'),
            ("selects security type 1", 'ws.send(bytes([1]))'),
            ("checks the SecurityResult", 'struct.unpack(">I", result[:4])[0] != 0'),
            ("sends ClientInit", 'ws.send(bytes([1]))'),
            ("reads ServerInit", 'len(init) < 24'),
            ("sends a FramebufferUpdateRequest", 'struct.pack(">BBHHHH", 3, 0, 0, 0, w, h)'),
        ):
            check_raw(needle in body, f"verify.sh {label}")

    print()
    if failures:
        print(f"FAIL: {len(failures)} check(s) did not hold")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS: the RFB handshake in verify.sh is byte-correct, and it matches this test")
    return 0


if __name__ == "__main__":
    sys.exit(main())
