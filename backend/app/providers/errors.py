from __future__ import annotations

from typing import Optional


class ProviderHTTPError(RuntimeError):
    """A provider answered with an HTTP error status.

    Raised instead of letting ``httpx.HTTPStatusError`` escape, because the
    distinction the UI has to make is not "did something go wrong" but *which
    stage failed*.  A 429 or a 503 means the provider was reached, read the
    request, and refused it on purpose.  Reporting that as "could not be
    reached" sends the reader looking at DNS, firewalls and API keys when the
    actual cause is a rate limit that resolves on its own after a wait.

    Carries what the provider actually said, because its error body is usually
    the only place the real reason appears: OpenRouter and Mistral both put a
    machine-readable code and a human sentence in there, and both are more
    specific than anything this layer could invent.

    Never carries a response that looks like a model reply.  A failed request
    has no completion, and synthesising one would put a fabricated command into
    a trace whose entire purpose is to be evidence.
    """

    def __init__(
        self,
        provider: str,
        model: str,
        status: int,
        reason: str = "",
        body: str = "",
        retry_after: Optional[float] = None,
    ) -> None:
        self.provider = provider
        self.model = model
        self.status = int(status)
        self.reason = reason or ""
        #: The provider's own error text, verbatim and unredacted of meaning.
        self.body = body or ""
        self.retry_after = retry_after
        if self.status == 429 and self.retry_after is None:
            self.retry_after = _retry_after_from_body(self.body)

        super().__init__(self.message)

    @property
    def reached(self) -> bool:
        """The provider answered, so this is a refusal rather than an outage."""
        return True

    @property
    def retryable(self) -> bool:
        """Whether waiting could plausibly change the answer.

        429 and 5xx are the server's own "later, please".  4xx otherwise are
        this request being wrong -- a bad key, a model that does not exist --
        and retrying those just burns the rate limit that was already scarce.
        """
        return self.status == 429 or 500 <= self.status <= 599

    @property
    def message(self) -> str:
        """The one-line summary the UI shows.

        Phrased to make the stage unambiguous: the provider was reached, here is
        the status it returned, and here is what it said.
        """
        status = f"HTTP {self.status}" + (f" {self.reason}" if self.reason else "")
        head = f"Provider reached — {status}"
        detail = self.provider_detail
        return f"{head}: {detail}" if detail else head

    @property
    def provider_detail(self) -> str:
        """The provider's error body, condensed to something displayable.

        Provider error bodies are JSON objects that wrap the sentence in a
        key nobody wants to read in a 400px panel, so the useful part is
        extracted when it can be.  When it cannot, the raw text is shown rather
        than dropped: "we could not parse this" is worse than "here is exactly
        what came back".
        """
        body = self.body.strip()
        if not body:
            return ""
        try:
            import json

            parsed = json.loads(body)
        except Exception:
            return _condense(body)
        return _from_json(parsed) or _condense(body)

    def to_dict(self) -> dict:
        """The structured form, for the trace."""
        return {
            "provider": self.provider,
            "model": self.model,
            "http_status": self.status,
            "http_reason": self.reason,
            "provider_error": self.provider_detail,
            "provider_error_raw": self.body,
            "retry_after": self.retry_after,
            "retryable": self.retryable,
            "reached": self.reached,
        }


def _retry_after_from_body(body: str) -> Optional[float]:
    """Extract compound provider reset delays such as 18m10.3s."""
    import re
    text = " ".join(str(body or "").split())
    patterns = (
        r"(?:try\s+again\s+in|wait\s+(?:for\s+)?)\s*"
        r"(?:(?P<h>[0-9]+(?:\.[0-9]+)?)\s*h\s*)?"
        r"(?:(?P<m>[0-9]+(?:\.[0-9]+)?)\s*m\s*)?"
        r"(?:(?P<s>[0-9]+(?:\.[0-9]+)?)\s*s)\b",
        r"retry\s+after\s+"
        r"(?:(?P<h2>[0-9]+(?:\.[0-9]+)?)\s*h\s*)?"
        r"(?:(?P<m2>[0-9]+(?:\.[0-9]+)?)\s*m\s*)?"
        r"(?:(?P<s2>[0-9]+(?:\.[0-9]+)?)\s*s)\b",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            try:
                g = match.groupdict()
                return max(
                    0.0,
                    float(g.get("h") or g.get("h2") or 0) * 3600
                    + float(g.get("m") or g.get("m2") or 0) * 60
                    + float(g.get("s") or g.get("s2") or 0),
                )
            except (TypeError, ValueError):
                pass
    return None


def _from_json(parsed: object) -> str:
    """Pull the human sentence out of a provider's JSON error body.

    OpenAI-compatible providers put it at ``error.message``; some nest it under
    ``detail`` or ``message``.  All three are checked, and anything else is left
    for the caller's fallback.
    """
    if not isinstance(parsed, dict):
        return ""
    for key in ("error", "detail", "message"):
        value = parsed.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            for inner in ("message", "detail", "code", "type"):
                got = value.get(inner)
                if isinstance(got, str) and got.strip():
                    return got.strip()
                if isinstance(got, int) and key == "code":
                    return str(got)
    return ""


def _condense(text: str, limit: int = 400) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "…"