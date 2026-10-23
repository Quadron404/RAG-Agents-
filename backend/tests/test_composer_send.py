"""The composer's Send must never fail silently.

The bug: the Send button was disabled whenever the socket was not ``online``,
and ``commit()`` returned before calling ``send()`` for the same reason.  So a
dropped connection left a visible-but-dead button, a silent Enter key, and a
draft that ``send()``'s own error toast never got the chance to explain.

These checks read the two frontend files that decide this and assert the shape
that fixes it: the button is not gated on the connection, ``commit()`` lets
``send()`` report the reason, and the draft is kept unless the message really
went out.

Run with:  python -m tests.test_composer_send
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CHAT = ROOT / "Frontend" / "src" / "views" / "Chat.tsx"
CORE = ROOT / "Frontend" / "src" / "core.ts"

failures: list[str] = []


def check(ok: bool, label: str) -> bool:
    print(("ok    " if ok else "FAIL  ") + label)
    if not ok:
        failures.append(label)
    return ok


def strip_comments(text: str) -> str:
    out = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("//") or stripped.startswith("*") or stripped.startswith("/*"):
            continue
        out.append(line)
    return "\n".join(out)


chat = CHAT.read_text(encoding="utf-8")
core = CORE.read_text(encoding="utf-8")
chat_code = strip_comments(chat)
core_code = strip_comments(core)

# --- the send no longer disappears when the socket is down -------------------
check(
    'if (busy || status !== "online") return;' not in chat_code,
    "commit() no longer bails out before send() when the socket is offline",
)
check(
    'disabled={!text.trim() || status !== "online"}' not in chat_code,
    "the Send button is no longer disabled by the connection status",
)
check(
    "disabled={!text.trim() && !attaches.length}" in chat_code,
    "Send is disabled only when there is nothing to send",
)

# --- commit() defers to send()'s own reporting ------------------------------
check(
    "if (!send(finalText)) return;" in chat_code,
    "commit() keeps the draft when send() reports it could not dispatch",
)
commit_start = chat_code.index("const commit = () => {")
commit_end = chat_code.index("\n  };", commit_start)
commit_body = chat_code[commit_start:commit_end]
check(
    commit_body.index("send(finalText)") < commit_body.index('setText("")'),
    "commit() clears the input only after send() has run",
)

# --- send() reports and returns a dispatch result ---------------------------
check(
    "send: (text: string) => boolean;" in core_code,
    "the store's send() is declared to return whether it dispatched",
)
check(
    'status !== "online" || !ws' not in core_code,
    "send() no longer depends on the computer-control socket",
)
send_start = core_code.index("  send(text) {")
send_body = core_code[send_start : core_code.index("\n  stopRun()", send_start)]
check(
    "Chief is already working on your request." in send_body,
    "send() explains when a request is already in flight",
)
check("return false;" in send_body, "the busy branch of send() returns false")
check("return true;" in send_body, "a dispatched message returns true")

# --- the composer's status line tells the truth -----------------------------
check(
    'status === "online" ? "dot--online" : "dot--offline"' in chat_code,
    "the composer hint shows the real connection state, not a fixed green dot",
)

if failures:
    print(f"\n{len(failures)} failed")
    raise SystemExit(1)
print("\nPASS: Send always submits or explains itself, and never drops the draft")
