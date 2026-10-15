"""Shared helpers for the auth-aware test modules.

Every module that drives the real FastAPI app through ``TestClient`` needs a
signed-in session, because the app no longer trusts an unauthenticated caller.
These helpers build that against the real app: the same code path a browser
cookie takes, so a test passing here is evidence the production route works.

The helpers also isolate the database: ``DATA_DIR`` is pointed at a fresh
temporary directory before ``app.main`` is imported, so a test never writes to
the developer's ``backend/data/rag.db``.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

_TEST_ROOT = Path(tempfile.mkdtemp(prefix="rag-test-data-"))
os.environ["DATA_DIR"] = str(_TEST_ROOT)
os.environ.setdefault("PUBLIC_URL_FILE", str(_TEST_ROOT / "public-url"))
os.environ["SESSION_SECRET"] = "test-session-secret"
os.environ["APP_ENCRYPTION_KEY"] = "test-encryption-key"

import app.main as main_module  # noqa: E402
from app.auth.deps import CSRF_HEADER, INTERNAL_HEADER  # noqa: E402

INTERNAL = "test-internal-token"


def db():
    return main_module.db


def sign_in(google_sub: str = "sub-1", email: str = "alice@example.com") -> dict:
    user = main_module.db.upsert_user(google_sub, email, True, "Alice", "")
    return user


def make_client(user_id: str, *, internal_token: str = INTERNAL) -> tuple:
    """A TestClient that holds a real session for ``user_id``.

    The client carries the internal token and the per-session CSRF token like a
    browser behind the Worker would.  Returns ``(client, session_record)``.
    """
    from fastapi.testclient import TestClient

    main_module.settings.rag_internal_token = internal_token
    session = main_module.auth.sessions.create(user_id)
    client = TestClient(main_module.app)
    from app.auth.sessions import COOKIE_NAME

    client.cookies.set(COOKIE_NAME, str(session["token"]))
    client.headers[INTERNAL_HEADER] = internal_token
    client.headers[CSRF_HEADER] = str(session["csrf"])
    return client, session


def attach_session(client, user_id: str = "ws-test-user", email: str = None) -> dict:
    """Give an existing TestClient a real session.

    Sets the session cookie and the per-session CSRF header, because the app
    enforces CSRF on state-changing methods and a browser behind the Worker
    always echoes it.  The header is harmless for the WebSocket tests that do
    not make HTTP verbs.
    """
    user = main_module.db.upsert_user(f"{user_id}-sub", email or f"{user_id}@example.com", True, "Tester", "")
    session = main_module.auth.sessions.create(user["id"])
    from app.auth.sessions import COOKIE_NAME

    client.cookies.set(COOKIE_NAME, str(session["token"]))
    client.headers[CSRF_HEADER] = str(session["csrf"])
    return session


def raw_client() -> tuple:
    """A client that holds the internal token but no user session at all."""
    from fastapi.testclient import TestClient

    main_module.settings.rag_internal_token = INTERNAL
    client = TestClient(main_module.app)
    client.headers[INTERNAL_HEADER] = INTERNAL
    return client