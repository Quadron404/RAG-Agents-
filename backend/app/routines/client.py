"""Thin, authenticated client for the edge's internal routines API.

The edge (Cloudflare Worker) is the authority for routines: it owns the D1 rows,
the recurrence clock and the run ledger, because all of that has to outlive this
backend.  So the tools here do not keep a database -- they call the Worker and
report what it says.  The shared ``x-rag-internal-token`` is the same secret the
Worker presents when it calls *this* backend, so the trust runs both ways and no
new credential is introduced.

Every method returns a plain ``{"status", "body"}`` record instead of raising.
The tools layer turns that into a sentence the model can read, and the scheduler
entry point turns a transport failure into "retryable" rather than pretending.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import httpx

from ..config import Settings

#: The Worker's own timeout, generous enough for a D1 round trip and small enough
#: that a stuck edge does not hold an agent turn open indefinitely.
_TIMEOUT_SECONDS = 15.0


class RoutineClient:
    def __init__(self, base_url: str, token: str, timeout: float = _TIMEOUT_SECONDS) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    @classmethod
    def from_settings(cls, settings: Settings) -> Optional["RoutineClient"]:
        """Build a client, or return None when the edge is not configured.

        ``worker_origin`` is preferred; ``public_base_url`` is the same origin the
        browser talks to and so is a correct fallback.  With neither, or with no
        shared token, routines cannot be managed at all, and the tools say so
        rather than inventing a schedule that nothing will ever run.
        """
        base = (settings.worker_origin or settings.public_base_url or "").strip().rstrip("/")
        token = (settings.rag_internal_token or "").strip()
        if not base or not token:
            return None
        return cls(base, token)

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        headers = {"x-rag-internal-token": self.token, "accept": "application/json"}
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.request(method, url, params=params, json=json_body, headers=headers)
        except httpx.HTTPError as exc:
            return {"status": 0, "body": {"ok": False, "error": f"edge unreachable: {exc}"}}
        try:
            body = response.json()
        except Exception:
            body = {"ok": False, "error": response.text[:500]}
        if not isinstance(body, dict):
            body = {"ok": False, "error": "unexpected edge response"}
        return {"status": response.status_code, "body": body}

    # --- routine management ------------------------------------------------

    async def create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return await self.request("POST", "/internal/routines", json_body=payload)

    async def list_for(self, owner_sub: str) -> Dict[str, Any]:
        return await self.request("GET", "/internal/routines", params={"owner_sub": owner_sub})

    async def get(self, routine_id: str, owner_sub: str) -> Dict[str, Any]:
        return await self.request(
            "GET", f"/internal/routines/{routine_id}", params={"owner_sub": owner_sub}
        )

    async def update(self, routine_id: str, patch: Dict[str, Any], owner_sub: str) -> Dict[str, Any]:
        return await self.request(
            "PATCH",
            f"/internal/routines/{routine_id}",
            params={"owner_sub": owner_sub},
            json_body=patch,
        )

    async def pause(self, routine_id: str, owner_sub: str) -> Dict[str, Any]:
        return await self.request(
            "POST", f"/internal/routines/{routine_id}/pause", params={"owner_sub": owner_sub}
        )

    async def resume(self, routine_id: str, owner_sub: str) -> Dict[str, Any]:
        return await self.request(
            "POST", f"/internal/routines/{routine_id}/resume", params={"owner_sub": owner_sub}
        )

    async def delete(self, routine_id: str, owner_sub: str) -> Dict[str, Any]:
        return await self.request(
            "DELETE", f"/internal/routines/{routine_id}", params={"owner_sub": owner_sub}
        )

    async def runs(self, routine_id: str, owner_sub: str, limit: int = 20) -> Dict[str, Any]:
        return await self.request(
            "GET",
            f"/internal/routines/{routine_id}/runs",
            params={"owner_sub": owner_sub, "limit": limit},
        )
