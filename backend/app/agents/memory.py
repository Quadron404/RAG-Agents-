"""Long-term memory with provenance, and the rules for what may be kept.

The legacy ``memory`` table holds bare key/value pairs with no indication of
where a value came from.  That is fine for a scratch note but wrong for
something the model is asked to trust: a fact the user stated and a guess the
model made must not be treated alike.  This module wraps the ``memory_facts``
table so every remembered value carries its ``source`` -- ``confirmed`` (the user
said it) or ``inferred`` (the model deduced it) -- and a confidence.

It also refuses to remember secrets.  A long-term store is the easiest place for
an API key or token to end up echoed back into every future prompt, so a value
that looks like a credential is rejected at the door rather than redacted after
the fact: what is never written cannot leak.
"""
from __future__ import annotations

import re
from typing import Dict, Iterable, List, Optional

from ..db import Database

CONFIRMED = "confirmed"
INFERRED = "inferred"

#: Shapes that mean "do not store this".  Deliberately broad: a false positive
#: costs one memory the user can restate, while a false negative puts a live
#: credential into a store that replays it into every subsequent prompt.
_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9]{16,}\b"),                 # OpenAI/Anthropic-style keys
    re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"),                # GitHub tokens
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),                    # AWS access keys
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),        # Slack tokens
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),  # JWTs
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._-]{8,}\b"),    # bearer tokens
    re.compile(r"(?i)\b(api[_ -]?key|secret|password|passwd|access[_ -]?token|refresh[_ -]?token|bearer)\b\s*[:=]"),
)

_SECRET_KEY_RE = re.compile(r"(?i)(password|passwd|secret|token|api[_-]?key|credential|auth)")


def looks_like_secret(value: str) -> bool:
    text = value or ""
    if any(pattern.search(text) for pattern in _SECRET_PATTERNS):
        return True
    return False


def remember(
    db: Database,
    user_id: str,
    key: str,
    value: str,
    *,
    source: str = INFERRED,
    confidence: float = 0.5,
) -> bool:
    """Store one fact unless it is empty, a secret, or unsafe to key.

    Returns whether it was stored, so a caller can tell an inferred guess that
    was quietly dropped (because it looked like a credential) from one that was
    kept, instead of assuming success.
    """
    key = (key or "").strip()
    value = (value or "").strip()
    if not user_id or not key or not value:
        return False
    if _SECRET_KEY_RE.search(key) or looks_like_secret(value):
        return False
    if source not in (CONFIRMED, INFERRED):
        source = INFERRED
    db.remember_fact(user_id, key, value, source=source, confidence=float(confidence))
    return True


def facts_for(db: Database, user_id: str, limit: int = 50) -> List[Dict[str, object]]:
    facts = db.recall_facts(user_id)
    return facts[: max(0, int(limit))]


def forget(db: Database, user_id: str, key: str) -> bool:
    """Remove one remembered fact.  Returns whether a fact was actually removed."""
    return db.forget_fact(user_id, key)


def render_facts(facts: Iterable[Dict[str, object]]) -> str:
    """A compact two-part block for the context.

    Confirmed facts are listed first and marked with ``=``; inferred ones with
    ``~``.  The distinction is visible to the model on purpose: it may treat a
    confirmed fact as settled and should treat an inferred one as something to
    re-check rather than rely on.
    """
    confirmed: List[str] = []
    inferred: List[str] = []
    for fact in facts:
        key = str(fact.get("key") or "").strip()
        value = str(fact.get("value") or "").strip()
        if not key or not value:
            continue
        line = f"{key} = {value}"
        if str(fact.get("source") or "").lower() == CONFIRMED:
            confirmed.append(line)
        else:
            inferred.append(line)
    parts: List[str] = []
    if confirmed:
        parts.append("Confirmed:\n" + "\n".join(f"- {line}" for line in confirmed))
    if inferred:
        parts.append("Inferred:\n" + "\n".join(f"- {line}" for line in inferred))
    return "\n".join(parts)


def memory_text_for(db: Database, user_id: str, limit: int = 50) -> str:
    return render_facts(facts_for(db, user_id, limit=limit))


__all__ = [
    "CONFIRMED",
    "INFERRED",
    "looks_like_secret",
    "remember",
    "forget",
    "facts_for",
    "render_facts",
    "memory_text_for",
]
