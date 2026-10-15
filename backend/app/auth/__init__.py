"""Server-side Google authentication and per-user authorization.

Everything security-sensitive lives in ordinary Python here, never in a model
prompt.  The pieces are deliberately small and independent so each can be
tested on its own:

- ``crypto``   authenticated encryption for secrets stored at rest.
- ``tokens``   pure JWT/JWKS verification (no provider library).
- ``google``   the OIDC authorization-code flow (state, nonce, PKCE).
- ``sessions`` hashed server-side sessions.
- ``deps``     the FastAPI dependencies every protected route uses.
"""
