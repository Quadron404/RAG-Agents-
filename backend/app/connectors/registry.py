"""Server-side connector registry.

Each connector defines its actions, input schemas, required OAuth permissions,
and the backend function that executes an action *after* owner approval.  A
model tool call is only a request -- it never executes a connector action
directly.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from ..auth.deps import AuthService
from ..config import Settings
from ..db import Database
from .gmail import GmailActionError, gmail_send_email

#: How long a pending approval lives before it expires (30 minutes).
APPROVAL_TTL_SECONDS = 30 * 60


@dataclass
class ConnectorAction:
    """One action a connector can perform, after owner approval."""

    id: str
    name: str
    description: str
    input_schema: Dict[str, Any]
    required_permissions: List[str] = field(default_factory=list)
    data_accessed: str = ""
    data_changed: str = ""
    execute: Optional[Callable[..., Any]] = None


@dataclass
class Connector:
    id: str
    name: str
    actions: Dict[str, ConnectorAction] = field(default_factory=dict)


#: All registered connectors.  Gmail is the first; Drive, Calendar, GitHub etc.
#: join by adding a Connector here and an execution adapter.
CONNECTORS: Dict[str, Connector] = {}


def register(connector: Connector) -> None:
    CONNECTORS[connector.id] = connector


def get_connector(connector_id: str) -> Optional[Connector]:
    return CONNECTORS.get(connector_id)


def list_connectors() -> List[Connector]:
    return list(CONNECTORS.values())


def tool_definitions() -> List[Dict[str, Any]]:
    """Groq/OpenAI-compatible tool definitions for every connector action.

    These are what the model can *request* -- execution happens only after the
    matching approval is granted.
    """
    tools: List[Dict[str, Any]] = []
    for connector in list_connectors():
        for action in connector.actions.values():
            tools.append({
                "type": "function",
                "function": {
                    "name": f"{connector.id}.{action.id}",
                    "description": action.description,
                    "parameters": action.input_schema,
                },
            })
    return tools


def resolve_tool_name(tool_name: str) -> Optional[tuple[str, str]]:
    """Split ``connector.action`` into (connector_id, action_id)."""
    if "." not in tool_name:
        return None
    connector_id, _, action_id = tool_name.partition(".")
    if connector_id in CONNECTORS and action_id in CONNECTORS[connector_id].actions:
        return connector_id, action_id
    return None


async def create_approval(
    db: Database,
    *,
    user_id: str,
    connector_id: str,
    action_id: str,
    arguments: Dict[str, object],
    routine_id: Optional[str] = None,
    routine_run_id: Optional[str] = None,
    ttl_seconds: int = APPROVAL_TTL_SECONDS,
) -> Dict[str, object]:
    """Create a pending owner-scoped one-time approval for a connector action.

    Returns the approval record (id, connector, action, arguments, expires_at)
    so the Worker can present the approval card to the owner.
    """
    approval_id = uuid.uuid4().hex
    expires_at = time.time() + ttl_seconds
    db.create_connector_approval(
        user_id=user_id,
        connector=connector_id,
        action=action_id,
        arguments=arguments,
        expires_at=expires_at,
        routine_id=routine_id,
        routine_run_id=routine_run_id,
    )
    return {
        "approval_id": approval_id,
        "connector": connector_id,
        "action": action_id,
        "arguments": arguments,
        "expires_at": expires_at,
        "status": "pending",
    }


async def execute_approved_action(
    db: Database,
    settings: Settings,
    auth_service: AuthService,
    *,
    approval_id: str,
    decided_by_user_id: str,
) -> Dict[str, object]:
    """Execute a connector action whose approval has just been granted.

    Revalidates the approval (owner, pending, not expired, matches the decided
    user), runs the connector's execute adapter, and records the outcome.
    """
    from .gmail import GmailActionError

    approval = db.get_connector_approval(approval_id)
    if not approval:
        return {"ok": False, "error": "no such approval", "approval_id": approval_id}
    if str(approval["user_id"]) != decided_by_user_id:
        return {"ok": False, "error": "approval does not belong to you", "approval_id": approval_id}
    if str(approval["status"]) != "pending":
        return {"ok": False, "error": f"approval is {approval['status']}", "approval_id": approval_id}
    if time.time() > float(approval["expires_at"]):
        db.resolve_connector_approval(approval_id, "expired", decided_by_user_id, "expired before decision")
        return {"ok": False, "error": "approval has expired", "approval_id": approval_id}

    resolved = resolve_tool_name(f"{approval['connector']}.{approval['action']}")
    if not resolved:
        return {"ok": False, "error": "unknown connector action", "approval_id": approval_id}
    connector_id, action_id = resolved
    connector = get_connector(connector_id)
    action = connector.actions[action_id] if connector else None
    if not action or not action.execute:
        return {"ok": False, "error": "connector action is not executable", "approval_id": approval_id}

    arguments = json.loads(str(approval["arguments"] or "{}"))
    try:
        outcome = await action.execute(
            db=db,
            settings=settings,
            auth_service=auth_service,
            user_id=str(approval["user_id"]),
            arguments=arguments,
        )
    except GmailActionError as exc:
        db.resolve_connector_approval(approval_id, "rejected", decided_by_user_id, f"failed: {exc}")
        return {"ok": False, "error": str(exc), "approval_id": approval_id}
    except Exception as exc:  # noqa: BLE001
        db.resolve_connector_approval(approval_id, "failed", decided_by_user_id, f"failed: {exc}")
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "approval_id": approval_id}

    db.resolve_connector_approval(approval_id, "allowed", decided_by_user_id, json.dumps(outcome))
    return {"ok": True, "result": outcome, "approval_id": approval_id}


# --- register Gmail ---------------------------------------------------------

from .gmail import GMAIL_CONNECTOR  # noqa: E402

register(GMAIL_CONNECTOR)
