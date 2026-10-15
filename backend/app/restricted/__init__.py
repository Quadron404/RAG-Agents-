"""Independently restricted browsing for ordinary agents.

This package is deliberately separate from ``app.computer``.  The computer
subsystem is a human-driven screen and a model that clicks it; an ordinary agent
is a text worker that may read the web.  They must not share a browser, a
profile or a network path, so the code that gives the ordinary agent a browser
lives here and never imports the computer controller.

Four pieces, each testable on its own:

``policy``
    The owner-controlled allowlist.  Default-deny, read-only to the agent.
``guard``
    The destination screen: scheme, host, port, DNS result and redirect, so a
    permitted hostname cannot smuggle in a private address or a bare IP.
``gateway``
    The enforcement point.  An allowlisting forward proxy; the sandbox's
    firewall makes it the only egress, so refusing here is refusing everywhere.
``runtime``
    What the tool layer talks to: the browser service and the fetch tool, both
    bound to the policy and the gateway.
"""

from .policy import Allowlist, Decision, Policy, load_policy  # noqa: F401
from .guard import Guard, GuardError  # noqa: F401
from .runtime import RestrictedRuntime  # noqa: F401

__all__ = [
    "Allowlist",
    "Decision",
    "Policy",
    "load_policy",
    "Guard",
    "GuardError",
    "RestrictedRuntime",
]
