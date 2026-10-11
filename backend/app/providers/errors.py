from __future__ import annotations

from typing import Optional, Dict, Any, Tuple


class ModelCallGateTimeout(RuntimeError):
    """A request waited for the model-call gate and the queue never cleared.

    Not an HTTP error, and deliberately not one: nothing was sent, so there is
    no status and no provider body to report, and inventing either would put a
    claim in the trace that no provider made.  It is the failure this whole class
    exists to prevent -- the one where a request is queued behind other model
    calls, never starts, and is indistinguishable from a provider that is slow.

    Raised instead of waiting forever, so the trace says *why* there is no reply:
    the request lost a queue, not the model.  The two usual reasons are a request
    already parked on a rate limit and another role (commander, worker, browser)
    streaming through the same gate.
    """

    def __init__(
        self,
        provider: str = "",
        model: str = "",
        waited: float = 0.0,
        held_by: str = "",
    ) -> None:
        self.provider = provider
        self.model = model
        self.waited = float(waited)
        #: Who holds the gate, when it is held.  Names the request that is
        #: actually in progress, which is the only thing that turns "queued" into
        #: something actionable.
        self.held_by = held_by or ""
        super().__init__(self.message)

    @property
    def message(self) -> str:
        who = f"{self.provider}/{self.model}" if self.provider and self.model else (
            self.provider or "the request"
        )
        held = f", still held by {self.held_by}" if self.held_by else ""
        # Deliberately not "the provider was never reached": this is raised from
        # two places, and only one of them is a request that was never sent.  The
        # other is a rate-limited retry that gave up before it was re-sent, where
        # the provider was reached and refused.  One sentence covering both is
        # "no reply came back", and the next says why.
        return (
            f"{who} gave up after waiting {self.waited:.0f}s for the model-call gate "
            f"shared by every model call in the app{held}. No reply came back: the "
            f"request was either never sent or, if the provider had already refused "
            f"it with a rate limit, never re-sent. The delay was this app's queue, "
            f"not the model."
        )


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
        headers: Optional[Dict[str, Any]] = None,
        limit_type: Optional[str] = None,
    ) -> None:
        self.provider = provider
        self.model = model
        self.status = int(status)
        self.reason = reason or ""
        #: The provider's own error text, verbatim and unredacted of meaning.
        self.body = body or ""
        self.headers = headers or {}
        self.limit_type = limit_type
        self.retry_after = retry_after
        self.retry_after_source = None

        if self.status == 429:
            self._compute_retry_after()
        else:
            if self.retry_after is not None:
                self.retry_after_source = "explicit"

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

    def _compute_retry_after(self) -> None:
        # Rate-limit headers are read regardless of which source provides the
        # delay, because they are the only place the *kind* of limit appears.
        # `x-ratelimit-reset-requests` and `x-ratelimit-reset-tokens` are two
        # different windows and are never interchangeable; the header parser
        # keeps them apart and reports which one (if either) it used.
        rl_seconds, rl_source, rl_limit_type = _retry_after_from_ratelimit_headers(
            self.headers
        )

        if self.retry_after is not None:
            # Caller supplied a usable Retry-After (usually the standard
            # header).  It wins, and is labelled by where it came from rather
            # than the generic word "explicit".
            self.retry_after_source = (
                "header:retry-after"
                if _retry_after_from_header(self.headers) is not None
                else "explicit"
            )
            if self.limit_type is None and rl_limit_type is not None:
                self.limit_type = rl_limit_type
            return

        # 1. Retry-After header (standard), authoritative when present
        ra_from_header = _retry_after_from_header(self.headers)
        if ra_from_header is not None:
            self.retry_after = ra_from_header
            self.retry_after_source = "header:retry-after"
            if self.limit_type is None and rl_limit_type is not None:
                self.limit_type = rl_limit_type
            return

        # 2. Rate-limit reset headers, request and token resets kept apart
        if rl_seconds is not None:
            self.retry_after = rl_seconds
            self.retry_after_source = rl_source or "header:ratelimit"
            if self.limit_type is None:
                self.limit_type = rl_limit_type
            return

        # 3. Fallback to the human sentence in the body, never inventing a time
        ra_from_body = _retry_after_from_body(self.body)
        if ra_from_body is not None:
            self.retry_after = ra_from_body
            self.retry_after_source = "body"
            return

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
            "retry_after_source": self.retry_after_source,
            "limit_type": self.limit_type,
            "retryable": self.retryable,
            "reached": self.reached,
        }


def _retry_after_from_header(headers: Dict[str, Any]) -> Optional[float]:
    """Parse standard Retry-After header (seconds or HTTP-date)."""
    if not headers:
        return None
    raw = ""
    try:
        # httpx headers are case-insensitive in practice, but be defensive
        for k, v in headers.items():
            if str(k).lower() == "retry-after":
                raw = str(v)
                break
    except Exception:
        return None
    raw = str(raw).strip()
    if not raw:
        return None
    try:
        seconds = float(raw)
        return max(0.0, seconds)
    except ValueError:
        try:
            from email.utils import parsedate_to_datetime
            from datetime import datetime, timezone

            when = parsedate_to_datetime(raw)
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            seconds = (when - datetime.now(timezone.utc)).total_seconds()
            return max(0.0, seconds)
        except Exception:
            return None


def _parse_reset_delay(raw: Optional[str]) -> Optional[float]:
    """A rate-limit reset header as seconds-from-now.

    Providers spell these three ways and all three must be read without
    guessing: a plain number of seconds (``90``), a duration string the same
    way the human message writes it (``1m30s``, ``27m41.2s``), or an absolute
    epoch timestamp (``1750000000``).  An absolute time in the past clamps to
    zero -- "come back now" -- and anything unreadable returns ``None`` so the
    caller falls through to another source rather than inventing a delay.
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        value = float(text)
    except ValueError:
        return _parse_duration(text)
    # Epoch scores are far larger than any plausible duration; treat only
    # those as absolute wall-clock times and convert to a delta.
    if value > 10**9:
        from datetime import datetime, timezone

        return max(0.0, value - datetime.now(timezone.utc).timestamp())
    return max(0.0, value)


def _parse_duration(text: str) -> Optional[float]:
    """Parse an ``h``/``m``/``s`` duration string such as ``1m30s``.

    The same compounds the provider writes in its errors, used for the reset
    headers that use the same spelling.  Returns ``None`` when nothing
    recognisable is present, so an unreadable header falls through rather than
    becoming a fabricated delay.
    """
    import re

    text = str(text or "").strip()
    if not text:
        return None
    match = re.fullmatch(
        r"\s*(?:(?P<h>[0-9]+(?:\.[0-9]+)?)\s*h\s*)?"
        r"(?:(?P<m>[0-9]+(?:\.[0-9]+)?)\s*m\s*)?"
        r"(?:(?P<s>[0-9]+(?:\.[0-9]+)?)\s*s)?\s*",
        text,
        re.IGNORECASE,
    )
    if not match or not any(match.groupdict().values()):
        return None
    try:
        g = match.groupdict()
        return max(
            0.0,
            float(g.get("h") or 0) * 3600
            + float(g.get("m") or 0) * 60
            + float(g.get("s") or 0),
        )
    except (TypeError, ValueError):
        return None


def _retry_after_from_ratelimit_headers(
    headers: Dict[str, Any]
) -> Tuple[Optional[float], Optional[str], Optional[str]]:
    """Read the ``x-ratelimit-*`` reset headers, request and token kept apart.

    ``x-ratelimit-reset-requests`` and ``x-ratelimit-reset-tokens`` are
    different windows and are never interchangeable, so this never substitutes
    one for the other.  When the exhausted counter is identifiable (its
    ``remaining`` header is ``0``) that limit's reset is the one returned; when
    it is not, the request reset is preferred because a request-cap 429 blocks
    every future call, whereas a token reset only affects the next payload.

    Returns ``(seconds, source, limit_type)``; any element may be ``None``.
    """
    if not headers:
        return None, None, None

    def get_header(name: str) -> Optional[str]:
        try:
            for k, v in headers.items():
                if str(k).lower() == name.lower():
                    return str(v)
        except Exception:
            pass
        return None

    reset_requests = get_header("x-ratelimit-reset-requests") or get_header(
        "ratelimit-reset-requests"
    )
    reset_tokens = get_header("x-ratelimit-reset-tokens") or get_header(
        "ratelimit-reset-tokens"
    )
    remaining_requests = get_header("x-ratelimit-remaining-requests") or get_header(
        "x-ratelimit-remaining-req"
    )
    remaining_tokens = get_header("x-ratelimit-remaining-tokens")

    def _zero(raw: Optional[str]) -> bool:
        try:
            return float(str(raw).strip()) <= 0
        except (TypeError, ValueError):
            return False

    requests_exhausted = _zero(remaining_requests)
    tokens_exhausted = _zero(remaining_tokens)

    if requests_exhausted and reset_requests:
        seconds = _parse_reset_delay(reset_requests)
        if seconds is not None:
            return seconds, "header:x-ratelimit-reset-requests", "requests_per_window"
    if tokens_exhausted and reset_tokens:
        seconds = _parse_reset_delay(reset_tokens)
        if seconds is not None:
            return seconds, "header:x-ratelimit-reset-tokens", "tokens_per_window"

    # Counter state did not identify the limit.  Prefer the request reset and
    # say so honestly, rather than silently picking the token one for it.
    if reset_requests:
        seconds = _parse_reset_delay(reset_requests)
        if seconds is not None:
            return seconds, "header:x-ratelimit-reset-requests", "requests_per_window"
    return None, None, None


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